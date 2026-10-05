# -*- coding: utf-8 -*-
"""服务探测与部署（官方扩展）

回答运维的第一句话：「这台机器上有什么、缺什么、能不能一键补上」。

- **探测**：probe 机制包找二进制 + 版本（TTL 缓存，且只缓存命中 —— 装完立即可见）；
  svcmgr 机制包按候选单元名逐个查服务状态（systemd / sc.exe 差异在包里收口）；
  pkg 机制包回答「这个文件是谁装的」—— 包管理器装的还是源码/手动部署的。
- **部署**：包名候选**按家族列举、按仓库实查**（candidate()），不是拍脑袋一个名字：
  - deb：Ubuntu 24.04 的 mysql-server 是 MySQL 8.4，Debian 13 只有 mariadb —— 候选序列自然兜住；
  - rpm：EL9 还是 redis，Fedora 41+/EL10 已被 valkey 取代 —— 候选序列覆盖更名；
  - EL 系默认仓库没有 php-fpm/mysql —— 候选全查不到时如实提示补源（EPEL/Remi）。
- **WARN 声明**：不兼容/未验证的组合绝不静默 —— os 识别 + pkg 家族 +
  winget/choco 可用性全部实查后生成 warnings 列表，前端原样亮出来。
- **边界**：只允许装本文件 CATALOG 白名单里的服务（包名不来自调用方）；
  Windows 探测/启停照常，安装取决于 winget/choco 是否存在（Server 2012 没有）。

命令：svc.list / svc.install {id} / svc.act {id, action}
"""
import logging
import os
import threading

logger = logging.getLogger("zernus")

__plugin_meta__ = {
    "name": "服务探测与部署",
    "version": "0.2.0",
    "author": "ZPanel",
    "desc": "服务清单（二进制/版本/服务单元/包归属）+ 白名单一键安装 + 启停 + 兼容性 WARN",
    "priority": 36,
    "official": True,
}

_probe = None
_svcmgr = None
_procs = None
_pkg = None

_INSTALL_LOCK = threading.Lock()      # 同时只允许一个安装（apt/dnf 并发会锁库互踩）

# 已验证过包名兼容性的发行版（ID 与 ID_LIKE 出现在这里 = catalog 包名有依据）
_KNOWN_OS = {
    "debian", "ubuntu", "armbian", "deepin", "uos", "kylin", "openkylin",
    "rhel", "centos", "rocky", "almalinux", "fedora", "anolis", "opencloudos",
    "tencentos", "tencentos-server", "tencent", "openeuler", "euleros",
    "sles", "suse", "opensuse", "arch", "manjaro", "alpine",
}

# ── 服务目录：id 只能从这里出现；pkgs 按家族给「候选序列」，装前逐个实查 ──
CATALOG = [
    {"id": "nginx", "name": "Nginx", "role": "Web 服务器",
     "exes": ["nginx"], "extra_paths": ["/usr/sbin", "/usr/local/nginx/sbin"],
     "vargs": ["-v"],
     "units": ["nginx", "nginx-compiled"],
     "pkgs": {"deb": ["nginx"], "rpm": ["nginx"],
              "pacman": ["nginx"], "apk": ["nginx"]},
     # 编译安装（aaPanel 双通道的另一半）：源码来自 nginx.org 官方，
     # --conf-path 复用发行版配置目录，站点配置不受影响；仅 nginx 提供，别的引擎补上再说
     "compile": {
         "url": "https://nginx.org/download/nginx-1.26.3.tar.gz",
         "prefix": "/usr/local/nginx",
         "conf_path": "/etc/nginx/nginx.conf",
         "unit": "nginx-compiled",
         "configure": ["--with-http_ssl_module", "--with-http_v2_module",
                       "--with-http_stub_status_module"],
         "deps": {"deb": ["build-essential", "libpcre2-dev", "libssl-dev", "zlib1g-dev"],
                  "rpm": ["gcc", "make", "pcre-devel", "zlib-devel", "openssl-devel"]}}},
    {"id": "apache", "name": "Apache httpd", "role": "Web 服务器",
     "exes": ["apache2", "httpd"], "extra_paths": ["/usr/sbin"],
     "vargs": ["-v"],
     "units": ["apache2", "httpd"],
     "pkgs": {"deb": ["apache2"], "rpm": ["httpd"],
              "pacman": ["apache"], "apk": ["apache2"]}},
    {"id": "php-fpm", "name": "PHP-FPM", "role": "PHP FastCGI",
     # Ubuntu/Debian 的二进制带版本号（php-fpm8.3），只找 php-fpm 会探空
     "exes": ["php-fpm", "php-fpm8.4", "php-fpm8.3", "php-fpm8.2",
              "php-fpm8.1", "php-fpm7.4"],
     "extra_paths": ["/usr/sbin"],
     "vargs": ["-v"],
     "units": ["php-fpm", "php8.4-fpm", "php8.3-fpm", "php8.2-fpm",
               "php8.1-fpm", "php7.4-fpm"],
     "pkgs": {"deb": ["php-fpm"], "rpm": ["php-fpm"],
              "pacman": ["php-fpm"], "apk": ["php-fpm", "php83-fpm"]},
     # EL 系（RHEL/CentOS/Anolis/TencentOS）基础仓库默认没有 php-fpm
     "hints": {"rpm": "EL 系基础仓库可能没有 php-fpm，需要 EPEL/Remi 等附加源"}},
    {"id": "mysql", "name": "MySQL / MariaDB", "role": "数据库",
     "exes": ["mysqld", "mariadbd"], "extra_paths": ["/usr/sbin"],
     "units": ["mysql", "mariadb", "mysqld", "MySQL80", "MySQL"],
     "pkgs": {"deb": ["mysql-server", "mariadb-server"],   # Ubuntu 是 MySQL，Debian 只有 MariaDB
              "rpm": ["mariadb-server"],                   # EL/Fedora 默认不带 MySQL
              "pacman": ["mariadb"], "apk": ["mariadb", "mariadb-server"]}},
    {"id": "postgresql", "name": "PostgreSQL", "role": "数据库",
     "exes": ["postgres"], "extra_paths": ["/usr/sbin"],
     "units": ["postgresql"],
     "pkgs": {"deb": ["postgresql"], "rpm": ["postgresql-server"],
              "pacman": ["postgresql"], "apk": ["postgresql"]}},
    {"id": "redis", "name": "Redis", "role": "缓存",
     "exes": ["redis-server", "valkey-server"], "extra_paths": ["/usr/bin"],
     "units": ["redis", "redis-server", "valkey", "valkey-server"],
     "pkgs": {"deb": ["redis-server"], "rpm": ["redis", "valkey"],   # Fedora 41+/EL10 已更名 valkey
              "pacman": ["redis"], "apk": ["redis"]},
     "hints": {"rpm": "Fedora 41+/EL10 已用 valkey 取代 redis（许可证变更），候选会自动落到 valkey"}},
    {"id": "memcached", "name": "Memcached", "role": "缓存",
     "exes": ["memcached"], "extra_paths": ["/usr/bin"],
     "units": ["memcached"],
     "pkgs": {"deb": ["memcached"], "rpm": ["memcached"],
              "pacman": ["memcached"], "apk": ["memcached"]}},
]
CATALOG_BY_ID = {e["id"]: e for e in CATALOG}


def _os_info() -> dict:
    """发行版识别：Linux 读 /etc/os-release（ID/ID_LIKE/VERSION_ID）；
    Windows 用 platform（Server 2012 build 9200、Win10 build 10240+）。"""
    if os.name == "nt":
        import platform as _p
        return {"system": "windows", "id": "windows",
                "pretty": f"Windows {_p.release()} (build {_p.version()})",
                "version_id": _p.version()}
    d = {}
    try:
        with open("/etc/os-release", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip().split("=", 1)
                    d[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return {"system": "linux", "id": d.get("ID", ""), "id_like": d.get("ID_LIKE", ""),
            "version_id": d.get("VERSION_ID", ""), "pretty": d.get("PRETTY_NAME", "")}


def _warnings(os_info: dict, pkg_backend: dict) -> list:
    """兼容性 WARN 声明 —— 每一条都来自真实检查，不猜、不静默。"""
    warns = []
    if os_info.get("system") == "windows":
        if not pkg_backend.get("available"):
            warns.append(
                "Windows 上未找到 winget/choco —— 服务探测与启停可用，一键安装不可用"
                "（Windows Server 2012/2012R2/2016 没有 winget）")
        try:
            build = int(str(os_info.get("version_id", "")).rsplit(".", 1)[-1])
            if build < 10240:
                warns.append(
                    f"旧版 Windows（build {build}，Server 2012 及更早）已停止官方支持，"
                    f"面板按探测结果如实展示，不保证所有能力可用")
        except ValueError:
            pass
        return warns

    fam = pkg_backend.get("family") or ""
    if not pkg_backend.get("available") or not fam:
        warns.append(f"未识别到受支持的包管理器（{os_info.get('pretty') or '未知系统'}）"
                     f"—— 一键安装不可用，仅可探测")
    ids = {os_info.get("id", ""), *(os_info.get("id_like") or "").split()}
    ids.discard("")
    if not ids & _KNOWN_OS:
        warns.append(f"未识别的发行版 {os_info.get('pretty') or '(未知)'}"
                     f"（ID={os_info.get('id') or '?'}）—— 包名兼容性未经验证，"
                     f"安装前请人工确认软件源")
    return warns


def _detect(entry: dict) -> dict:
    """单个服务的真实状态 —— 每个字段都必须来自实际检查，不做推断：

    - exe/version：probe 真实找文件、真实执行二进制取版本（TTL 只缓存命中）；
    - unit/running：svcmgr 真实 ``systemctl status`` / ``sc query`` 逐候选查询；
    - pkg：pkg.owns() 实查文件归属 —— 区分包管理器装的与源码/手动部署的；
    - state：running/stopped 只在「服务单元存在」时才下结论；
      有二进制但没注册服务单元 → ``bare``（运行状态未知），绝不冒充"已停止"。
    """
    _vargs = entry.get("vargs")
    info = _probe.probe(entry["id"], exes=entry["exes"],
                        args=(tuple(_vargs) if _vargs else None),
                        extra_paths=tuple(entry.get("extra_paths") or ()))
    exe = info.get("exe") or ""
    version = str(info.get("version") or "")

    unit, running, unit_err = "", None, ""
    try:
        svc = _svcmgr.resolve(entry["units"])
        if svc.get("found"):
            unit = svc.get("name") or ""
            running = bool(svc.get("running"))
    except Exception as e:      # 平台没有服务管理器
        unit_err = f"{type(e).__name__}: {e}"
        logger.debug("[services] svcmgr.resolve(%s) 失败: %s", entry["id"], unit_err)

    owner = ""
    if exe:
        try:
            owner = (_pkg.owns(exe) or {}).get("pkg") or ""
        except Exception:
            owner = ""

    if unit:
        state = "running" if running else "stopped"
    elif exe:
        state = "bare"          # 二进制在、服务单元无 —— 不推断运行状态
    else:
        state = "missing"

    return {"id": entry["id"], "name": entry["name"], "role": entry["role"],
            "exe": exe, "version": version, "unit": unit,
            "state": state, "running": running, "unit_error": unit_err,
            "pkg_owner": owner,                      # 空 = 非包管理器安装（源码/手动）
            "installable": bool(entry.get("pkgs"))}


def _pick_candidates(entry: dict, family: str) -> dict:
    """在仓库索引里逐个实查候选包名，返回 {chosen, tried, refresh}。

    全部落空时刷新一次索引再试（新装机的 apt lists 可能是空的）；
    仍落空则如实报"需要补软件源"，绝不拿第一个名字去碰运气。
    """
    cands = (entry.get("pkgs") or {}).get(family) or []
    if not cands:
        return {"chosen": "", "tried": [], "refresh": None,
                "error": f"{entry['name']} 没有配置 {family} 家族的包名候选"}

    def _first_hit():
        for c in cands:
            cc = _pkg.candidate(c)
            if cc.get("exists"):
                return c, cc
        return "", None

    chosen, hit = _first_hit()
    refresh_r = None
    if not chosen:
        refresh_r = _pkg.refresh()
        chosen, hit = _first_hit()
    return {"chosen": chosen, "tried": cands, "refresh": refresh_r,
            "version": (hit or {}).get("version", ""),
            "error": "" if chosen else
            f"仓库索引里找不到候选包（已试：{', '.join(cands)}）"
            f"—— 可能需要补软件源（EPEL/Remi 等）后再试"}


def _install(entry_id: str, timeout: float = 900.0,
             task=None, task_id: str = None) -> dict:
    """白名单服务一键安装：家族判定 → 候选实查 → 锁检查 → 安装 → **装后必须校验**。

    task/task_id 由任务队列注入（见 svc.install），执行过程逐段写进度日志 ——
    装服务是分钟级操作，不写进度就是黑盒。
    """
    def _tlog(msg: str):
        try:
            if task is not None and task_id:
                task.log(task_id, msg)
        except Exception:
            pass

    entry = CATALOG_BY_ID.get(str(entry_id or "").strip())
    if entry is None:
        return {"ok": False, "error": f"未知服务 id: {entry_id}（只允许目录内的白名单服务）"}

    _tlog(f"开始安装 {entry['name']}，识别包管理器家族…")
    family = _pkg.family()
    if not family:
        _tlog("未识别到受支持的包管理器，终止")
        return {"ok": False, "error": "未识别到受支持的包管理器，无法安装"}
    _tlog(f"包管理器家族：{family}（{_pkg.backend().get('backend')}）")

    pick = _pick_candidates(entry, family)
    if not pick.get("chosen"):
        _tlog(f"候选包全落空（已试：{', '.join(pick.get('tried') or [])}）")
        return {"ok": False, "candidates": pick.get("tried") or [],
                "refresh_log": (pick.get("refresh") or {}).get("log", ""),
                "error": pick.get("error") or "候选包不可用"}
    _tlog(f"仓库实查选定：{pick['chosen']}（candidate {pick.get('version') or '?'}）")

    st = _pkg.lock_status()
    if st.get("busy"):
        _tlog(f"包管理器被占用（{st.get('holder')}），终止")
        return {"ok": False, "lock_busy": True,
                "error": f"包管理器正被占用（{st.get('holder') or '其他进程'}），稍后再试"}

    chosen = pick["chosen"]
    if not _INSTALL_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有安装任务在进行中，请等它结束"}
    try:
        logger.info("[services] 安装 %s（%s）: %s", entry["id"], family, chosen)
        _tlog(f"执行安装：{chosen}（最长等待 {int(timeout)}s）…")
        r = _pkg.install([chosen], timeout=timeout)
        out = str(r.get("log") or "")
        _tlog(f"安装命令结束：returncode={r.get('returncode')}"
              f"{'（超时）' if r.get('timed_out') else ''}")
        # 装后校验：包管理器说装上了 + 本机真的查得到，两条都过才算成功
        inst = _pkg.installed(chosen)
        verified = bool(inst.get("installed"))
        _tlog(f"装后校验（{chosen} 实查）：{'通过 ' + str(inst.get('version') or '') if verified else '未找到该包'}")
        item = _detect(entry)
        _tlog(f"最终状态：{item.get('state')}，版本 {item.get('version') or '—'}")
        ok = bool(r.get("ok")) and verified
        if not ok:
            logger.warning("[services] 安装 %s 失败: %s", entry["id"], out[-300:])
        return {"ok": ok, "pkg": chosen, "family": family,
                "returncode": r.get("returncode"),
                "verified": verified,
                "install_log": out[-2000:], "item": item,
                "error": "" if ok else
                (out[-400:] or "安装命令执行成功但未在本机确认到该包")}
    finally:
        _INSTALL_LOCK.release()


def _act(entry_id: str, action: str) -> dict:
    """启停重启：单元名来自探测结果，动作白名单由 svcmgr 再验一层。"""
    entry = CATALOG_BY_ID.get(str(entry_id or "").strip())
    if entry is None:
        return {"ok": False, "error": f"未知服务 id: {entry_id}"}
    if action not in ("start", "stop", "restart", "reload", "enable", "disable"):
        return {"ok": False, "error": f"不支持的动作: {action}"}
    try:
        svc = _svcmgr.resolve(entry["units"])
    except Exception as e:
        return {"ok": False, "error": f"服务管理器不可用: {e}"}
    if not svc.get("found"):
        return {"ok": False, "error": f"{entry['name']} 未注册为系统服务（unit 不存在）"}
    return _svcmgr.act(action, svc["name"], timeout=40.0)


def _uninstall(entry_id: str, timeout: float = 600.0) -> dict:
    """卸载：先停服务 → 算出该卸哪些包（owner + 已装候选）→ 卸 → **卸后必须复核**。

    不用 --purge（保留 /etc 配置）；源码/手动安装的（pkg.owns 查不到 owner、
    候选包也未装）如实拒绝 —— 面板不删它没装过的东西。
    """
    entry = CATALOG_BY_ID.get(str(entry_id or "").strip())
    if entry is None:
        return {"ok": False, "error": f"未知服务 id: {entry_id}"}
    family = _pkg.family()
    if not family:
        return {"ok": False, "error": "未识别到受支持的包管理器，无法卸载"}

    # ① 停服务（单元在才停；不在就跳过）
    try:
        svc = _svcmgr.resolve(entry["units"])
        if svc.get("found") and svc.get("running"):
            _svcmgr.act("stop", svc["name"], timeout=40.0)
    except Exception:
        pass

    # ② 要卸的包：二进制归属包 + 目录候选里已安装的，去重
    names: list = []
    _vargs = entry.get("vargs")
    info = _probe.probe(entry["id"], exes=entry["exes"],
                        args=(tuple(_vargs) if _vargs else None),
                        extra_paths=tuple(entry.get("extra_paths") or ()))
    exe = info.get("exe") or ""
    if exe:
        owner = (_pkg.owns(exe) or {}).get("pkg") or ""
        if owner:
            names.append(owner)
    for c in (entry.get("pkgs") or {}).get(family) or []:
        if c not in names and _pkg.installed(c).get("installed"):
            names.append(c)
    if not names:
        return {"ok": False, "error":
                f"没有找到属于包管理器的 {entry['name']} —— 可能是源码/手动安装，"
                f"请手工移除（面板不删它没装过的东西）"}

    # ③ 卸载 + ④ 复核
    r = _pkg.remove(names, timeout=timeout)
    _probe.clear_cache()                      # 装卸探测缓存立即失效，别报旧状态
    still = any(_pkg.installed(n).get("installed") for n in names)
    item = _detect(entry)
    ok = bool(r.get("ok")) and not still
    return {"ok": ok, "removed_pkgs": names, "verified_removed": not still,
            "returncode": r.get("returncode"), "log": str(r.get("log") or "")[-2000:],
            "item": item,
            "error": "" if ok else str(r.get("error") or "卸载命令执行了但包还在")}


def _compile_nginx(task=None, task_id: str = None, timeout: float = 1800.0) -> dict:
    """编译安装 nginx（源码：nginx.org 官方 tarball → prefix /usr/local/nginx）。

    --conf-path 指回发行版配置（/etc/nginx/nginx.conf），站点配置不受影响；
    装完注册 systemd 单元 nginx-compiled，与包管理器版互斥 —— 共存会抢 80/配置，先卸再编。
    """
    def _tlog(msg: str):
        try:
            if task is not None and task_id:
                task.log(task_id, msg)
        except Exception:
            pass

    import tarfile as _tar
    import urllib.request as _req

    entry = CATALOG_BY_ID["nginx"]
    spec = entry.get("compile") or {}
    family = _pkg.family()
    if not family:
        return {"ok": False, "error": "未识别到受支持的包管理器（编译依赖需要先装）"}

    # 0) 冲突守卫：包管理器版还装着就拒绝 —— 两份 nginx 抢 80 和配置，不是能共存的
    _vargs = entry.get("vargs")
    cur = _probe.probe("nginx", exes=entry["exes"], args=(tuple(_vargs),),
                       extra_paths=tuple(entry.get("extra_paths") or ()))
    if cur.get("exe") and not str(cur.get("exe")).startswith("/usr/local/"):
        owner = (_pkg.owns(cur["exe"]) or {}).get("pkg") or ""
        if owner:
            return {"ok": False, "error":
                    f"包管理器版 nginx（{owner}）还在，先卸载再编译安装 —— 两份 nginx 抢 80 端口"}

    _tlog("① 安装编译依赖（gcc/make/pcre/openssl）…")
    deps = (spec.get("deps") or {}).get(family) or []
    if deps:
        r = _pkg.install(deps, timeout=timeout)
        if not r.get("ok"):
            return {"ok": False, "error": f"编译依赖安装失败：{str(r.get('error') or '')[-300]}"}
    _tlog("① 编译依赖就绪")

    url = spec.get("url") or ""
    tgz = "/tmp/nginx-src.tar.gz"
    _tlog(f"② 下载源码：{url}")
    try:
        with _req.urlopen(url, timeout=180) as resp, open(tgz, "wb") as f:
            data = resp.read(64 * 1024 * 1024)
            f.write(data)
    except Exception as e:
        return {"ok": False, "error": f"源码下载失败: {e}（可检查网络或换镜像）"}
    _tlog(f"② 下载完成：{len(data) // 1024} KB")

    src_root = "/usr/local/src"
    _tlog("③ 解包源码…")
    # 源码目录以 tarball 第一个成员为准 —— 别用 listdir 前缀猜
    # （曾经 /usr/local/src 下的 nginx-build 空目录抢了 nginx-1.26.3 的位置）
    with _tar.open(tgz) as tf:
        top = tf.getmembers()[0].name.split("/")[0]
        tf.extractall(src_root)
    src_dir = os.path.join(src_root, top)

    prefix = spec.get("prefix") or "/usr/local/nginx"
    conf_path = spec.get("conf_path") or "/etc/nginx/nginx.conf"
    argv = ["./configure", f"--prefix={prefix}", f"--conf-path={conf_path}",
            *(spec.get("configure") or [])]
    import multiprocessing
    jobs = str(max(1, multiprocessing.cpu_count()))

    def _run(argv2, cwd=None, what=""):
        _tlog(f"→ {what}: {' '.join(argv2[:4])}…")
        r = _procs.run(argv2, cwd=cwd, timeout=timeout, shell=False)
        ok = bool(r.ok)
        _tlog(f"{'✓' if ok else '✗'} {what}（rc={r.returncode}）")
        if not ok:
            _tlog(((r.stdout or "") + (r.stderr or ""))[-600:])
        return ok

    _tlog(f"④ configure（prefix={prefix}，conf 复用 {conf_path}）…")
    if not _run(argv, cwd=src_dir, what="configure"):
        return {"ok": False, "error": "configure 失败（缺编译依赖？日志见任务输出）"}
    _tlog(f"⑤ make -j{jobs}（这是最长的一步，ARM 上约几分钟）…")
    if not _run(["make", f"-j{jobs}"], cwd=src_dir, what="make"):
        return {"ok": False, "error": "make 失败（日志见任务输出）"}
    _tlog("⑥ make install…")
    if not _run(["make", "install"], cwd=src_dir, what="make install"):
        return {"ok": False, "error": "make install 失败"}

    # ⑦ systemd 单元（区别于包管理器版的 nginx.service，避免互抢）
    unit_name = spec.get("unit") or "nginx-compiled"
    unit = (f"[Unit]\nDescription=Nginx (compiled by zpanel)\nAfter=network.target\n\n"
            f"[Service]\nType=forking\n"
            f"ExecStart={prefix}/sbin/nginx\n"
            f"ExecReload={prefix}/sbin/nginx -s reload\n"
            f"ExecStop={prefix}/sbin/nginx -s quit\n"
            f"Restart=on-failure\n\n[Install]\nWantedBy=multi-user.target\n")
    with open(f"/etc/systemd/system/{unit_name}.service", "w", encoding="utf-8") as f:
        f.write(unit)
    _tlog(f"⑦ 注册 systemd 单元 {unit_name} 并启动…")
    _procs.run(["systemctl", "daemon-reload"], timeout=30, shell=False)
    _procs.run(["systemctl", "enable", "--now", unit_name], timeout=60, shell=False)

    _probe.clear_cache()
    item = _detect(entry)
    _tlog(f"⑧ 最终状态：{item.get('state')}，{item.get('exe') or '?'}（编译版）")
    return {"ok": item.get("state") == "running", "prefix": prefix,
            "unit": unit_name, "item": item,
            "error": "" if item.get("state") == "running" else "编译安装完成但服务未运行，查看单元日志"}


def register(ctx):
    global _probe, _svcmgr, _procs, _pkg
    fw = ctx._framework
    cfg = fw.config.get("services") or {}
    if cfg.get("enabled", True) is False:
        ctx.log("服务探测已禁用 (services.enabled: false)")
        return

    _probe = ctx.zkg_tool("probe")
    _svcmgr = ctx.zkg_tool("svcmgr")
    _procs = ctx.zkg_tool("procs")
    _pkg = ctx.zkg_tool("pkg")
    missing = [n for n, m in (("probe", _probe), ("svcmgr", _svcmgr),
                              ("procs", _procs), ("pkg", _pkg)) if m is None]
    if missing:
        raise RuntimeError(
            f"服务探测缺少机制包 {missing} —— 检查 software/extensions/services/"
            f"manifest.toml 的 dependencies")

    def _h_list(a=None):
        os_info = _os_info()
        pkg_backend = _pkg.backend()
        items = [_detect(e) for e in CATALOG]
        return {"ok": True, "data": {"items": items, "pkg": pkg_backend,
                                     "os": os_info,
                                     "warnings": _warnings(os_info, pkg_backend)}}

    def _h_install(a):
        """一键安装 = 提交**后台任务**（装服务是分钟级操作，不该占着请求等）。

        返回 task_id；进度经 task.get / 任务中心查看。执行体在收到命令的
        那台节点上（localhost 即本机任务队列，远程即该节点的队列）。
        没有任务队列时退回同步执行，不假装有任务。
        """
        data = a or {}
        entry_id = str(data.get("id") or "").strip()
        entry = CATALOG_BY_ID.get(entry_id)
        if entry is None:
            return {"ok": False, "data": {"error": f"未知服务 id: {entry_id}"}}
        tq = getattr(fw, "task_queue", None)
        if tq is None:
            r = _install(entry_id, timeout=float(data.get("timeout") or 900.0))
            return {"ok": bool(r.get("ok")), "data": r}

        def _run(task_id=None):
            return _install(entry_id,
                            timeout=float(data.get("timeout") or 900.0),
                            task=tq, task_id=task_id)

        tid = tq.submit(_run, name=f"安装 {entry['name']}",
                        meta={"kind": "svc.install", "service": entry_id})
        return {"ok": True, "data": {"task_id": tid, "id": entry_id,
                                     "name": entry["name"], "async": True}}

    def _h_install_run(a):
        """同步执行体（任务队列的 runner 之外，供程序化调用/排障）。"""
        r = _install((a or {}).get("id", ""),
                     timeout=float((a or {}).get("timeout") or 900.0))
        return {"ok": bool(r.get("ok")), "data": r}

    def _h_uninstall(a):
        """卸载（同步：卸载是十秒级操作，不需要任务化）。"""
        r = _uninstall((a or {}).get("id", ""),
                       timeout=float((a or {}).get("timeout") or 600.0))
        return {"ok": bool(r.get("ok")), "data": r}

    def _h_compile(a):
        """编译安装 = 后台任务（分钟级，进度逐段写日志）。仅目录里声明 compile 的服务。"""
        data = a or {}
        entry_id = str(data.get("id") or "").strip()
        entry = CATALOG_BY_ID.get(entry_id)
        if entry is None or not entry.get("compile"):
            return {"ok": False, "data": {"error": f"{entry_id} 不支持编译安装"}}
        tq = getattr(fw, "task_queue", None)
        if tq is None:
            return {"ok": False, "data": {"error": "任务队列不可用，编译安装必须走任务"}}

        def _run(task_id=None):
            return _compile_nginx(task=tq, task_id=task_id,
                                  timeout=float(data.get("timeout") or 1800.0))

        tid = tq.submit(_run, name=f"编译安装 {entry['name']}",
                        meta={"kind": "svc.compile", "service": entry_id})
        return {"ok": True, "data": {"task_id": tid, "id": entry_id,
                                     "name": entry["name"], "async": True}}

    def _h_act(a):
        r = _act((a or {}).get("id", ""), str((a or {}).get("action") or ""))
        return {"ok": bool(r.get("ok")), "data": r}

    fw.nodes.register_handler("svc.list", _h_list, desc="服务清单与状态 + 兼容性 WARN",
                              level="software")
    fw.nodes.register_handler("svc.install", _h_install,
                              desc="一键安装（后台任务，返回 task_id）", level="software")
    fw.nodes.register_handler("svc.install.run", _h_install_run,
                              desc="一键安装（同步执行体）", level="software")
    fw.nodes.register_handler("svc.uninstall", _h_uninstall,
                              desc="卸载（先停服务，包归属实查）", level="software")
    fw.nodes.register_handler("svc.compile", _h_compile,
                              desc="编译安装（后台任务，仅声明 compile 的服务）", level="software")
    fw.nodes.register_handler("svc.act", _h_act, desc="服务启停", level="software")

    fam = _pkg.family()
    ctx.log(f"服务探测已就绪：目录 {len(CATALOG)} 项，包管理器家族 {fam or '未识别'}"
            f"（{_pkg.backend().get('backend')}），服务管理器 {_svcmgr.backend().get('backend')}")
