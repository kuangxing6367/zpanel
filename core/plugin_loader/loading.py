"""
单插件加载 / register 调用 / 命令·任务·卡片同步 / 心跳·自检 mixin

负责把插件目录动态导入为模块、调用其 register(ctx)、把命令/定时任务/
仪表盘卡片同步到数据库与调度器，并提供每分钟心跳增量注册与孤儿清理。
"""
import gc
import importlib.util
import logging
import os
import sys
import threading

from .pip import pip_install_all
from .source_loader import _PluginSourceLoader
from core.hooks import HookPoints

logger = logging.getLogger('zernus')


class LoadingMixin:
    """插件加载、注册与运行时同步。"""

    def load_plugin(self, plugin_name: str) -> bool:
        """加载单个插件，返回是否成功"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name)

        # 将插件目录加入 sys.path
        if plugin_path not in sys.path:
            sys.path.insert(0, plugin_path)

        # 读取插件配置文件
        yaml_data = self.read_plugin_yaml(plugin_name)

        # ====== 依赖检查 + 自动安装（基于全局环境） ======
        # 说明：框架不再为插件自动创建/重建隔离虚拟环境；
        # 依赖自动安装到全局环境，存在版本冲突的依赖自动跳过（不覆盖全局包），
        # 如需隔离请手动点击「创建虚拟环境」。
        dep_result = self.check_dependencies(plugin_name)
        if dep_result.get('missing'):
            pip_install_all(plugin_name, dep_result['missing'])

            # 重新检查依赖
            dep_result = self.check_dependencies(plugin_name)
            if dep_result.get('missing'):
                missing = ', '.join(dep_result['missing'])
                logger.warning(f"[{plugin_name}] 仍有缺失依赖: {missing}，尝试加载...")

        # 记录依赖状态供 Web UI 展示
        self._record_dep_status(
            plugin_name,
            dep_result.get('missing', []),
            dep_result.get('conflicts', [])
        )

        # ====== 动态导入 main.py（带重试）======
        main_path = os.path.join(plugin_path, 'main.py')
        for attempt in range(2):  # 最多重试1次
            try:
                # 第二次尝试前清掉首次残留的半初始化模块，保证重试幂等
                if attempt == 1:
                    self._purge_plugin_modules(plugin_name, plugin_path)

                # 0) 清掉本插件 __pycache__，避免同秒同尺寸改动复用旧字节码
                self._clear_plugin_bytecode_cache(plugin_path)

                # 1) 先建合成包 plugin_<插件名>（含 __package__/__path__）：
                #    它既是 main.py 的执行载体，也是插件相对导入的父包
                module = self._ensure_plugin_package(plugin_name, plugin_path)

                # 2) 预加载顶层子模块（点分层级名 + 下划线唯一名 + 短名，同名模块互不污染）
                self._preload_plugin_submodules(plugin_name, plugin_path)

                # 3) main.py 直接执行进合成包模块（不能再 module_from_spec，
                #    否则刚设置的 __package__/__path__ 会重置为普通顶层模块，相对导入又会失效）
                spec = importlib.util.spec_from_file_location(
                    f"plugin_{plugin_name}",
                    main_path,
                    loader=_PluginSourceLoader(f"plugin_{plugin_name}", main_path)
                )
                if spec is None or spec.loader is None:
                    logger.error(f"[{plugin_name}] 导入失败: spec 为空")
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    return False

                module.__spec__ = spec
                module.__loader__ = spec.loader
                spec.loader.exec_module(module)

                # 检查 register 函数
                if not hasattr(module, 'register'):
                    logger.error(f"[{plugin_name}] 缺少 register(ctx) 函数")
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    return False

                register_func = getattr(module, 'register')
                if not callable(register_func):
                    logger.error(f"[{plugin_name}] register 不可调用")
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    return False

                # 读取元数据
                meta = getattr(module, '__plugin_meta__', {})
                plugin_meta = {
                    'name': meta.get('name', plugin_name),
                    'version': meta.get('version', '0.0.0'),
                    'author': meta.get('author', 'unknown'),
                    'desc': meta.get('desc', ''),
                    'priority': meta.get('priority', 50),
                }

                # 读取 plugin.yaml 覆盖元数据
                if yaml_data:
                    if 'version' in yaml_data:
                        plugin_meta['version'] = yaml_data['version']
                    if 'author' in yaml_data:
                        plugin_meta['author'] = yaml_data['author']
                    if 'description' in yaml_data:
                        plugin_meta['desc'] = yaml_data['description']
                    if 'priority' in yaml_data:
                        plugin_meta['priority'] = yaml_data['priority']

                with self._lock:
                    self._loaded_plugins[plugin_name] = {
                        'module': module,
                        'register_func': register_func,
                        'meta': plugin_meta,
                        'priority': plugin_meta['priority'],
                        'path': plugin_path,
                        'yaml': yaml_data,
                    }

                self._upsert_plugin_db(plugin_name, plugin_meta)
                self.init_plugin_configs(plugin_name)
                # 记录文件快照，避免首个心跳周期重复注册
                self._plugin_mtimes[plugin_name] = self._snapshot_mtime(plugin_name)

                logger.info(f"[{plugin_name}] 加载成功 v{plugin_meta['version']}")
                return True

            except ImportError as e:
                logger.warning(f"[{plugin_name}] 导入失败（第{attempt + 1}次）: {e}")
                if attempt == 0:
                    # 第一次失败：尝试重新安装缺失依赖（全局环境）再试一次
                    logger.info(f"[{plugin_name}] 尝试重新安装缺失依赖...")
                    dep_result2 = self.check_dependencies(plugin_name)
                    if dep_result2.get('missing'):
                        pip_install_all(plugin_name, dep_result2['missing'])
                    continue
                else:
                    logger.error(
                        f"[{plugin_name}] 加载失败，依赖可能未正确安装\n"
                        f"  请检查: pip install {' '.join(dep_result.get('missing', []))}\n"
                        f"  或在 Web UI 插件管理页查看详情"
                    )
                    # 回滚 sys.modules 中残留的合成包与子模块
                    self._purge_plugin_modules(plugin_name, plugin_path)
                    # 更新 DB 状态为 error
                    try:
                        self.db.execute(
                            "UPDATE plugins SET status='error', has_register=0 WHERE plugin_name=%s",
                            (plugin_name,)
                        )
                    except Exception:
                        pass
                    return False

            except Exception as e:
                logger.error(f"[{plugin_name}] 加载失败: {e}", exc_info=True)
                # 回滚 sys.modules 中残留的合成包与子模块
                self._purge_plugin_modules(plugin_name, plugin_path)
                # 更新 DB 状态为 error
                try:
                    self.db.execute(
                        "UPDATE plugins SET status='error', has_register=0 WHERE plugin_name=%s",
                        (plugin_name,)
                    )
                except Exception:
                    pass
                return False

    def _upsert_plugin_db(self, plugin_name: str, meta: dict):
        """写入/更新插件信息到 plugins 表"""
        try:
            existing = self.db.query_one(
                "SELECT id FROM plugins WHERE plugin_name = %s", (plugin_name,)
            )
            if existing:
                self.db.execute(
                    "UPDATE plugins SET version=%s, author=%s, description=%s, priority=%s, "
                    "has_register=1, status='running', loaded_at=NOW() WHERE plugin_name=%s",
                    (meta['version'], meta['author'], meta['desc'], meta['priority'], plugin_name)
                )
            else:
                self.db.execute(
                    "INSERT INTO plugins (plugin_name, version, author, description, priority, "
                    "has_register, status, loaded_at) VALUES (%s,%s,%s,%s,%s,1,'running',NOW())",
                    (plugin_name, meta['version'], meta['author'], meta['desc'], meta['priority'])
                )
        except Exception as e:
            logger.error(f"写入插件数据库失败 [{plugin_name}]: {e}")

    def register_commands(self, plugin_name: str) -> bool:
        """
        调用插件的 register(ctx)，收集其注册的命令
        由心跳或加载时调用
        """
        with self._lock:
            info = self._loaded_plugins.get(plugin_name)
            if not info:
                return False

        from core.ctx.context import PluginContext
        ctx = PluginContext(plugin_name, self.framework)

        # 将 ctx 注入到插件模块的全局变量中
        # 这样插件的处理函数可以直接使用 ctx.api() 等
        module = info['module']
        module.ctx = ctx

        try:
            info['register_func'](ctx)
        except Exception as e:
            logger.error(f"[{plugin_name}] register(ctx) 执行异常: {e}", exc_info=True)
            return False

        # 获取注册的命令和任务
        commands = ctx._get_commands()
        tasks = ctx._get_tasks()
        dashboard_cards = ctx._get_dashboard_cards()

        # 注册原始消息处理器（收到原始消息事件，可选择性接管）
        raw_handlers = ctx._get_raw_message_handlers()
        if raw_handlers:
            _priority = info.get('priority', 50)
            for _h in raw_handlers:
                self.framework.register_raw_message_handler(plugin_name, _h, _priority)

        # 收集 WebUI 群组/用户管理页插件扩展
        group_exts = ctx._get_group_extensions()
        if group_exts:
            with self._lock:
                info['group_extensions'] = group_exts
        user_exts = ctx._get_user_extensions()
        if user_exts:
            with self._lock:
                info['user_extensions'] = user_exts

        # 写入 commands 表
        if commands:
            self._sync_commands(plugin_name, commands)

        # 注册定时任务
        if tasks:
            self._sync_tasks(plugin_name, tasks)

        # 存储仪表盘卡片
        if dashboard_cards:
            self._sync_dashboard_cards(plugin_name, dashboard_cards)

        logger.info(f"[{plugin_name}] 注册完成: {len(commands)} 命令, {len(tasks)} 定时任务")

        # 生命周期钩子：插件首次加载完成后触发一次（重载会重新触发）
        try:
            if not info.get('hook_loaded'):
                on_loaded = getattr(module, 'on_loaded', None)
                if callable(on_loaded):
                    on_loaded(ctx)
                info['hook_loaded'] = True
        except Exception as e:
            logger.error(f"[{plugin_name}] on_loaded 钩子异常: {e}")

        # 命令已写入 DB，让路由表立即重建（热路径内存快照要求一致）
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass

        # 扩展点：插件加载/注册完成（含心跳重载会重新触发）
        try:
            self.framework.hooks.trigger_sync(HookPoints.PLUGIN_LOAD, plugin_name)
        except Exception as e:
            logger.error(f"[{plugin_name}] 扩展点 [plugin.load] 触发异常: {e}")

        return True

    def _sync_commands(self, plugin_name: str, commands: list):
        """
        同步命令到数据库
        心跳策略：INSERT ... ON DUPLICATE KEY UPDATE 保持 ID 不变
        保留用户在 Web 端修改的 alias/description/is_active 覆盖（通过 handler_name 匹配回填）
        注意：is_dynamic 标记仅表示命令由插件以 dynamic=True 注册，不影响同步策略
              真正的动态命令（关键词回复）存储在 dynamic_commands 表，不受此处影响
        """
        try:
            # 查询当前数据库中所有命令的用户覆盖（按 handler_name 索引）
            existing_overrides = {}
            try:
                rows = self.db.query(
                    "SELECT handler, alias, description, is_active FROM commands "
                    "WHERE plugin_name = %s",
                    (plugin_name,)
                )
                for r in rows:
                    existing_overrides[r['handler']] = {
                        'alias': r.get('alias'),
                        'description': r.get('description'),
                        'is_active': r.get('is_active', 1),
                    }
            except Exception:
                pass

            # INSERT ... ON DUPLICATE KEY UPDATE 保持 ID 不变
            if commands:
                # require_perm 列由 db 迁移添加；极老库可能没有，失败则回退到不含该列的写法
                base_cols = ("plugin_name, pattern, alias, description, "
                             "priority, handler, is_dynamic, require_level, is_active")
                base_upd = ("pattern = VALUES(pattern), alias = VALUES(alias), "
                            "description = VALUES(description), priority = VALUES(priority), "
                            "is_dynamic = VALUES(is_dynamic), require_level = VALUES(require_level), "
                            "is_active = VALUES(is_active)")
                params = []
                for c in commands:
                    handler_name = c['handler_name']
                    override = existing_overrides.get(handler_name, {})
                    # 优先使用用户在 Web 端设置的 alias，否则用代码注册的 alias
                    final_alias = override.get('alias') if override.get('alias') is not None else c.get('alias')
                    final_desc = override.get('description') if override.get('description') is not None else c.get('description')
                    final_active = override.get('is_active', 1)
                    params.append((
                        c['plugin_name'], c['pattern'], final_alias, final_desc,
                        c['priority'], handler_name, c.get('is_dynamic', 0),
                        c.get('require_level', ''), final_active
                    ))

                if self._commands_has_require_perm():
                    sql = (
                        f"INSERT INTO commands ({base_cols}, require_perm) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        f"ON DUPLICATE KEY UPDATE {base_upd}, "
                        "require_perm = VALUES(require_perm)"
                    )
                    params = [p + ((c.get('require_perm') or ''),)
                              for p, c in zip(params, commands)]
                else:
                    sql = (
                        f"INSERT INTO commands ({base_cols}) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        f"ON DUPLICATE KEY UPDATE {base_upd}"
                    )
                self.db.execute_many(sql, params)
        except Exception as e:
            logger.error(f"[{plugin_name}] 同步命令失败: {e}")

    def _commands_has_require_perm(self) -> bool:
        """commands 表是否已有 require_perm 列（兼容未执行迁移的极老库）"""
        cached = getattr(self, '_cmd_has_perm_col', None)
        if cached is not None:
            return cached
        try:
            has = self.db.table_has_column('commands', 'require_perm')
        except Exception:
            has = False
        self._cmd_has_perm_col = has
        return has

    def _sync_tasks(self, plugin_name: str, tasks: list):
        """同步定时任务到数据库和调度器"""
        try:
            # 先移除调度器中的旧任务（避免僵尸残留 + 重复添加报错）
            self.framework.scheduler.remove_plugin_tasks(plugin_name)

            # 删除旧任务
            self.db.execute(
                "DELETE FROM tasks WHERE plugin_name = %s", (plugin_name,)
            )
            # 插入新任务
            for t in tasks:
                task_id = self.db.insert(
                    "INSERT INTO tasks (plugin_name, cron_expression, handler, description) VALUES (%s,%s,%s,%s)",
                    (t['plugin_name'], t['cron_expression'], t['handler_name'], t['description'])
                )
                t['id'] = task_id
                # 注册到调度器
                self.framework.scheduler.add_plugin_task(t)

        except Exception as e:
            logger.error(f"[{plugin_name}] 同步任务失败: {e}")

    def _snapshot_mtime(self, plugin_name: str) -> float:
        """计算插件目录下所有 .py 文件的最大修改时间，用于变更检测"""
        plugin_path = os.path.join(self.plugins_dir, plugin_name)
        if not os.path.isdir(plugin_path):
            return -1.0
        latest = 0.0
        try:
            for root, _dirs, files in os.walk(plugin_path):
                for f in files:
                    if f.endswith('.py'):
                        try:
                            latest = max(latest, os.path.getmtime(os.path.join(root, f)))
                        except OSError:
                            pass
        except OSError:
            pass
        return latest

    def heartbeat_register(self):
        """
        心跳检查：仅对用户插件（plugins/ 目录）中文件发生变化的插件重新 register(ctx)
        核心插件（extensions/）已在启动时注册，不参与心跳
        """
        with self._lock:
            plugin_names = list(self._loaded_plugins.keys())

        changed = []
        for name in plugin_names:
            info = self._loaded_plugins.get(name, {})
            plugin_path = info.get('path', '')
            # 只处理用户插件目录下的插件
            if not plugin_path.startswith(self.plugins_dir):
                continue
            snap = self._snapshot_mtime(name)
            if snap == self._plugin_mtimes.get(name):
                continue
            try:
                self.register_commands(name)
                self._plugin_mtimes[name] = snap
                changed.append(name)
                logger.debug(f"[{name}] 心跳增量注册完成")
            except Exception as e:
                logger.error(f"[{name}] 心跳注册异常: {e}")

        # 心跳后使路由缓存失效（命令可能有变化）
        if changed:
            try:
                self.framework.router._invalidate_cache()
            except Exception:
                pass
        # 无论是否发生变化，心跳后也刷新一次路由表（兜底：DB 与内存对齐）
        try:
            self.framework.router._invalidate_cache()
        except Exception:
            pass

    def self_check_orphans(self):
        """
        周期性自检（默认随心跳每分钟执行一次）：清理不应存在的孤儿任务/命令。

        判定规则：
        - 数据库中 tasks / commands 表存在「插件代码目录已不存在（未被 discover）」的
          条目时，视为孤儿，直接从库表删除（任务同时移除调度器注册）。
        - 调度器中属于「当前未加载插件」的任务（无法执行），从调度器移除。

        这样即使插件被手动删除、卸载异常或禁用流程未完全清理，也能自动校正，
        避免「幽灵任务」继续触发。已禁用/已卸载插件在 unload 时已清过库表，
        此处作为兜底，不会误删仍存在的插件数据。
        """
        try:
            with self._lock:
                loaded = set(self._loaded_plugins.keys())
            discovered = set(self.discover())
            if not discovered and not loaded:
                return

            # 1) 数据库孤儿清理：插件目录已不存在的 tasks / commands
            #    表名来自固定白名单，非外部输入，可安全格式化
            for tbl in ('tasks', 'commands'):
                try:
                    rows = self.db.query(
                        f"SELECT DISTINCT plugin_name FROM {tbl}"
                    )
                except Exception as e:
                    logger.warning(f"[自检] 查询 {tbl} 失败: {e}")
                    continue
                for r in rows:
                    pn = r.get('plugin_name')
                    if not pn or pn in discovered:
                        continue
                    try:
                        self.db.execute(
                            f"DELETE FROM {tbl} WHERE plugin_name = %s", (pn,)
                        )
                        if tbl == 'tasks':
                            self.framework.scheduler.remove_plugin_tasks(pn)
                        logger.info(f"[自检] 清理孤儿 {tbl}: 插件 [{pn}] 已不存在")
                    except Exception as e:
                        logger.warning(f"[自检] 删除 {tbl} [{pn}] 失败: {e}")

            # 2) 调度器孤儿清理：任务所属插件当前未加载，无法执行则移除
            try:
                scheduler = self.framework.scheduler
                if scheduler is not None:
                    jobs = getattr(scheduler, '_jobs', None)
                    if isinstance(jobs, dict):
                        stale = [
                            tid for tid, info in jobs.items()
                            if info.get('plugin_name') not in loaded
                        ]
                        for tid in stale:
                            try:
                                scheduler.remove_job(tid)
                                logger.info(f"[自检] 移除调度器孤儿任务: {tid}")
                            except Exception:
                                pass
                    else:
                        # 当前调度器没有内存任务清单（如系统级计划任务扩展），跳过
                        logger.debug("[自检] 调度器无内存任务清单，跳过孤儿清理")
            except Exception as e:
                logger.warning(f"[自检] 调度器孤儿清理失败: {e}")

            # 3) 路由表兜底刷新（孤儿命令删除后保证内存与 DB 对齐）
            try:
                self.framework.router._invalidate_cache()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[自检] 异常: {e}")

    def is_plugin_active_in_db(self, plugin_name: str) -> bool:
        """
        检查插件在数据库中是否处于「启用」状态
        无记录视为启用（首次发现、尚未写入 plugins 表的插件默认启用）
        """
        try:
            row = self.db.query_one(
                "SELECT is_active FROM plugins WHERE plugin_name = %s", (plugin_name,)
            )
        except Exception:
            return True
        if row is None:
            return True
        return bool(row.get('is_active', 1))

    def load_all(self) -> list:
        """加载所有已发现插件，返回成功列表

        已在数据库中标记为禁用（is_active=0）的插件会被跳过，
        避免「框架重启后仍然加载已禁用插件」的问题。
        """
        discovered = self.discover()
        success = []
        for name in discovered:
            if not self.is_plugin_active_in_db(name):
                logger.info(f"[{name}] 插件已被禁用（is_active=0），跳过加载")
                continue
            if self.load_plugin(name):
                success.append(name)
        # 启动内存监控
        self._start_memory_monitor()
        return success

    def _start_memory_monitor(self):
        """
        启动内存监控线程（每 3 秒采样一次）
        监控进程总内存和估算每个插件模块的内存占用
        连续两次超过阈值则自动卸载插件
        """
        if self._memory_monitor_running:
            return
        self._memory_monitor_running = True

        plugin_cfg = self.framework.config.get('plugin', {}) or {}
        # 单插件上限：plugin.max_memory_mb（超限计数，连续 2 次自动卸载）
        per_plugin_mb = int(plugin_cfg.get('max_memory_mb', 64) or 64)
        # 进程级上限：与内核/服务级看门狗同源 service.watchdog.max_memory_mb
        wd_cfg = ((self.framework.config.get('service') or {}).get('watchdog') or {})
        proc_limit_mb = int(wd_cfg.get('max_memory_mb', 256) or 256)

        def monitor():
            try:
                import psutil  # 延迟导入：psutil 导入较慢，仅在启动时加载
                process = psutil.Process(os.getpid())
            except ImportError:
                process = None
                logger.warning("[内存监控] 未安装 psutil，跳过进程级内存监控（仍做插件级估算）")

            while self._memory_monitor_running:
                threading.Event().wait(3)

                try:
                    # 进程级内存监控（psutil 可选）
                    if process is not None:
                        proc_mem = process.memory_info().rss / 1024 / 1024
                        if proc_mem > proc_limit_mb * 1.5:  # 进程总内存超过 1.5 倍阈值
                            logger.warning(
                                f"[内存监控] 进程内存 {proc_mem:.1f}MB 超过警戒线 "
                                f"({proc_limit_mb * 1.5:.0f}MB)，可能存在插件泄漏"
                            )

                    # 逐个插件粗略估计内存（通过模块全局变量大小）
                    with self._lock:
                        for name, info in list(self._loaded_plugins.items()):
                            try:
                                module = info['module']
                                # 估算：模块的 __dict__ 里所有对象大小之和
                                module_size = sum(
                                    sys.getsizeof(v) for v in
                                    vars(module).values()
                                    if not v.__class__.__name__.startswith(('module', 'function', 'type'))
                                ) / 1024 / 1024

                                if module_size > per_plugin_mb:
                                    count = self._memory_violations.get(name, 0) + 1
                                    self._memory_violations[name] = count
                                    if count >= 2:
                                        logger.error(
                                            f"[内存监控] [{name}] 连续 {count} 次超限 "
                                            f"({module_size:.1f}MB > {per_plugin_mb}MB)，自动卸载"
                                        )
                                        # 异步卸载（不在此线程内执行耗时操作）
                                        threading.Thread(
                                            target=self.unload_plugin,
                                            args=(name,),
                                            daemon=True,
                                        ).start()
                                    else:
                                        logger.warning(
                                            f"[内存监控] [{name}] 内存使用 {module_size:.1f}MB "
                                            f"超过限制 {per_plugin_mb}MB（第 {count} 次警告）"
                                        )
                                else:
                                    # 恢复正常，清除违规计数
                                    self._memory_violations.pop(name, None)

                            except Exception:
                                pass  # 单个插件估算失败不影响其他

                except Exception:
                    pass  # 监控异常不干扰主流程

        t = threading.Thread(target=monitor, daemon=True, name="memory_monitor")
        t.start()
        logger.info(
            f"内存监控线程已启动 (采样间隔 3s, 单插件上限 {per_plugin_mb}MB, "
            f"进程上限 {proc_limit_mb}MB)"
        )
