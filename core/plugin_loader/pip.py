"""
pip 安装工具与版本说明符解析（模块级工具函数）

- 清华源 + 自动回退的 pip 安装；
- 宽松的 PEP 440 版本说明符解析与兼容性判断；
- requirements.txt 解析。
供依赖检查、隔离虚拟环境、插件加载等 mixin 复用。
"""
import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger('zernus')


# ── pip 镜像源列表（按优先级，第一个是清华源，后续是回退）─────────────
# 全部为 https：不加 --trusted-host（那会禁用证书校验，给劫持安装开洞）
_PIP_MIRRORS = [
    'https://pypi.tuna.tsinghua.edu.cn/simple',
    'https://mirrors.aliyun.com/pypi/simple',
    'https://pypi.org/simple',  # 官方源（最后回退）
]


def pip_install_with_mirror(pip_exec, packages, timeout=120) -> dict:
    """
    使用清华源安装 pip 包，失败自动回退到下一个镜像源
    :param pip_exec: pip 可执行路径（如 sys.executable 或 venv 内的 pip）
    :param packages: 要安装的包列表（如 ['requests>=2.28']）、单个包名字符串、或 requirements 文件路径
    :param timeout: 单次安装超时（秒）
    :return: {'success': bool, 'mirror': str, 'error': str}
    """
    install_args = [pip_exec, '-m', 'pip', 'install']
    packages_list = []

    if isinstance(packages, str):
        pkg = packages
        # 判断是否为 requirements 文件路径（以 .txt 结尾的路径）
        if pkg.endswith('.txt') and os.path.isfile(pkg):
            install_args.extend(['-r', pkg])
        else:
            # 普通包名字符串（如 'requests>=2.28'）
            install_args.append(pkg)
        packages_list = [pkg]
    else:
        # 列表：逐项处理，识别 .txt 文件路径，转换为 -r 参数
        for pkg in packages:
            if isinstance(pkg, str) and pkg.endswith('.txt') and os.path.isfile(pkg):
                install_args.extend(['-r', pkg])
            else:
                install_args.append(pkg)
            packages_list.append(pkg)

    last_error = ''
    for mirror in _PIP_MIRRORS:
        try:
            cmd = install_args + ['-i', mirror]
            logger.info(f"pip 安装中（镜像: {mirror}）: {packages_list}")
            subprocess.check_call(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return {'success': True, 'mirror': mirror, 'error': ''}
        except subprocess.CalledProcessError as e:
            last_error = f"exit {e.returncode}"
            logger.warning(f"pip 安装失败（镜像 {mirror}）: {last_error}，尝试下一个镜像源...")
        except subprocess.TimeoutExpired:
            last_error = f"超时({timeout}s)"
            logger.warning(f"pip 安装超时（镜像 {mirror}）: {last_error}，尝试下一个镜像源...")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"pip 安装异常（镜像 {mirror}）: {last_error}")

    return {'success': False, 'mirror': '', 'error': last_error}


def pip_install_all(plugin_name: str, deps: list):
    """批量安装依赖到当前 Python 环境"""
    logger.info(f"[{plugin_name}] 安装依赖到当前环境: {', '.join(deps)}")
    for dep in deps:
        try:
            r = pip_install_with_mirror(sys.executable, dep, timeout=120)
            if r['success']:
                logger.info(f"[{plugin_name}] 依赖安装成功: {dep}")
            else:
                logger.warning(f"[{plugin_name}] 依赖安装失败: {dep} - {r.get('error')}")
        except Exception as e:
            logger.warning(f"[{plugin_name}] 依赖安装异常: {dep} - {e}")


def pip_install_requirements(pip_exec, req_file, timeout=300) -> dict:
    """
    安装 requirements.txt，走清华源 + 回退
    :param pip_exec: pip 可执行路径
    :param req_file: requirements.txt 文件路径
    :param timeout: 超时（秒）
    :return: {'success': bool, 'mirror': str, 'error': str}
    """
    last_error = ''
    for mirror in _PIP_MIRRORS:
        try:
            cmd = [pip_exec, '-m', 'pip', 'install', '-r', req_file, '-i', mirror]
            logger.info(f"pip 安装 requirements（镜像: {mirror}）: {req_file}")
            subprocess.check_call(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return {'success': True, 'mirror': mirror, 'error': ''}
        except subprocess.CalledProcessError as e:
            last_error = f"exit {e.returncode}"
            logger.warning(f"requirements 安装失败（镜像 {mirror}）: {last_error}")
        except subprocess.TimeoutExpired:
            last_error = f"超时({timeout}s)"
            logger.warning(f"requirements 安装超时（镜像 {mirror}）: {last_error}")
        except Exception as e:
            last_error = str(e)

    return {'success': False, 'mirror': '', 'error': last_error}


# ── 版本说明符解析 ────────────────────────────────────────────────

# 只提取包名部分（第一个非空格且不含运算符的连续单词）
_RE_PKG_NAME = re.compile(r'^([a-zA-Z0-9_.-]+)')
# 匹配简单的 单运算符+版本 格式，如 >=2.28, ==1.0.0
_RE_SIMPLE_SPEC = re.compile(r'\s*(>=|<=|!=|~=|==|>|<)\s*([\d.*]+)')


def _parse_version_spec(dep: str):
    """
    解析依赖版本说明符（宽松模式）
    只提取包名，版本约束如果无法解析则跳过版本检查并记录警告
    返回 (包名, 运算符, 版本号) 或 (包名, None, None)
    例: 'requests>=2.28'  → ('requests', '>=', '2.28')
        'numpy<2.0'       → ('numpy', '<', '2.0')
        'psutil'          → ('psutil', None, None)
        'requests~=2.28.0, <3.0' → ('requests', None, None)  # 复杂条件跳过
    """
    dep = dep.strip()
    m = _RE_PKG_NAME.match(dep)
    if not m:
        return (dep, None, None)
    pkg = m.group(1)

    spec_part = dep[len(pkg):].strip()
    if not spec_part:
        return (pkg, None, None)

    vm = _RE_SIMPLE_SPEC.match(spec_part)
    if vm:
        return (pkg, vm.group(1), vm.group(2))

    # 复杂格式（如 ~=3.0.0, <4 或带逗号的多条件）→ 记录警告，跳过版本检查
    logger.warning(
        f"依赖版本约束 '{dep}' 格式复杂，框架将跳过版本检查，"
        f"请手动确认兼容性：pip install '{dep}'"
    )
    return (pkg, None, None)


def _parse_ver(v: str):
    """版本字符串 → 可比较元组，如 '2.28.1' → (2, 28, 1)"""
    parts = []
    for p in v.split('.'):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(p)
    return tuple(parts)


def _check_version_compatible(installed: str, operator: str, required: str) -> bool:
    """检查已安装版本是否满足运算符要求"""
    if operator is None:
        return True
    iv = _parse_ver(installed)
    rv = _parse_ver(required.rstrip('.*'))
    rlen = len(rv)

    if operator == '==':
        return iv[:rlen] == rv
    elif operator == '>=':
        return iv >= rv
    elif operator == '<=':
        return iv <= rv
    elif operator == '>':
        return iv > rv
    elif operator == '<':
        return iv < rv
    elif operator == '!=':
        return iv[:rlen] != rv
    elif operator == '~=':
        # ~=3.0   → >=3.0, <4.0
        # ~=3.0.0 → >=3.0.0, <3.1.0
        if rlen == 1:
            return iv >= rv and iv < (rv[0] + 1,)
        elif rlen == 2:
            return iv >= rv and iv < (rv[0] + 1,)
        else:
            return iv >= rv and iv < (rv[0], rv[1] + 1)
    return True


def _parse_requirements_file(req_file: str) -> list:
    """
    解析 requirements.txt 文件，返回依赖项列表（去重、保留顺序）
    跳过空行、注释行和不规范的行
    """
    if not os.path.isfile(req_file):
        return []
    result = []
    seen = set()
    try:
        with open(req_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # 跳过空行和注释
                if not line or line.startswith('#'):
                    continue
                # 跳过 -r / -e / --index-url 等 pip 选项行
                if line.startswith('-') or line.startswith('--'):
                    continue
                # 去掉行内注释（pkg # comment 形式）
                if ' #' in line:
                    line = line.split(' #', 1)[0].strip()
                if not line:
                    continue
                # 标准化为小写 key 做去重，保留原始写法
                norm = line.lower()
                if norm not in seen:
                    seen.add(norm)
                    result.append(line)
    except Exception as e:
        logger.warning(f"解析 requirements.txt 失败 [{req_file}]: {e}")
        return []
    return result
