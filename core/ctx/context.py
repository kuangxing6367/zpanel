"""
PluginContext 装配（core/ctx 节点 13）

把按职责拆分的 mixin 组装为单一 PluginContext 类，实例接口与拆分前完全一致。
框架/插件侧统一通过 `from core.ctx import PluginContext` 使用。
"""

from .base import PluginContextBase
from .registration import RegistrationMixin
from .permission import PermissionMixin
from .config import ConfigMixin
from .api import ApiMixin
from .messaging import MessagingMixin
from .roles import RolesMixin
from .database import DatabaseMixin
from .audit import AuditMixin
from .webui import WebUiMixin
from .session import SessionMixin
from .files import FileMixin
from .http import HttpMixin
from .serialize import SerializeMixin
from .jobs import JobMixin
from .eventbus_ext import EventBusExtMixin
from .cache import CacheMixin
from .di import DIMixin


class PluginContext(
    RegistrationMixin,
    PermissionMixin,
    ConfigMixin,
    ApiMixin,
    MessagingMixin,
    RolesMixin,
    DatabaseMixin,
    AuditMixin,
    WebUiMixin,
    SessionMixin,
    FileMixin,
    HttpMixin,
    SerializeMixin,
    JobMixin,
    EventBusExtMixin,
    CacheMixin,
    DIMixin,
    PluginContextBase,
):
    """插件上下文，传递给 register(ctx) 函数。

    构成（按依赖顺序）：
    - base           共享状态 + 基础属性（onebot/logger/plugin_name/_current_bot/get_data_dir/log/run_async）
    - registration   命令/任务/事件注册（command/task/on/on_raw_message/emit/aemit/hook/unhook/get_text）
    - permission     权限组查询（has_perm/check_perm/user_groups）
    - config         插件配置读取（get_config/get_all_config）
    - api            自定义 API 路由 + 协议 API 调用（register_api/call_async/api/aapi）
    - messaging       群动作快捷方法（send_msg/ban/kick/mute_all/set_card/get_member_* ...）
    - roles          身份判定 + 群级插件开关（is_group_admin/.../enable_plugin_in_group/...）
    - database       数据库连接池操作（db_query*/create_table/db_connection/db_*_async/pool_status）
    - audit          插件操作审计（audit_log）
    - webui          WebUI 注册 + 注册期集合访问器
    - session        多轮会话（wait_for/create_session）
    - files          文件读写（read_file/write_file/list_dir，限定插件数据目录）
    - http           HTTP 客户端（http_get/http_post + 异步版，标准库 urllib）
    - serialize      JSON/YAML 序列化（load_json/dump_json/load_yaml/dump_yaml）
    - jobs           定时任务（add_job/remove_job，直连 scheduler）
    - eventbus_ext   事件总线扩展（once/off/await_event）
    - cache          进程内缓存（cache_get/cache_set/cache_delete）
    - di             依赖注入（provide/inject）
    """

    __slots__ = ()


__all__ = ['PluginContext']
