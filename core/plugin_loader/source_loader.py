"""
插件源码加载器
始终从 .py 源码现场编译，不读取也不写入 __pycache__，从根上保证「磁盘是什么就跑什么」。
"""
import importlib.machinery


class _PluginSourceLoader(importlib.machinery.SourceFileLoader):
    """
    插件模块专用加载器：始终从 .py 源码现场编译，不读取也不写入 __pycache__。

    背景：CPython 默认按“源码整数秒 mtime + 文件大小”校验 .pyc。热重载/自动
    改码时若在同一秒内把文件改成相同字节数，会误判字节码仍有效而执行旧代码。
    插件加载频率很低，直接每次从源码编译最稳妥，从根上保证“磁盘是什么就跑什么”。
    （深层嵌套包由原生 finder 沿合成包 __path__ 懒加载，其 __pycache__ 另由
    _clear_plugin_bytecode_cache 在加载前清理。）
    """

    def get_code(self, fullname):
        source_path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(source_path), source_path)

    def set_data(self, *args, **kwargs):
        # 不生成 .pyc
        return None
