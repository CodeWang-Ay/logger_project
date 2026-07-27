import os
import yaml
import logging
import coloredlogs
import threading
from watchdog.observers     import Observer
from watchdog.events        import FileSystemEventHandler
from logging.handlers       import TimedRotatingFileHandler

class LoggerManager:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.CONFIG: dict = {}
        self.config_lock = threading.Lock()
        self.observer = None

        # 初始化加载配置
        self.reload_config()

    def _load_yaml(self, file_path: str) -> dict:
        """加载 YAML 文件并返回其内容。"""
        with open(file_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}

    def reload_config(self) -> None:
        """重新加载配置文件到全局 CONFIG。"""
        try:
            new_config = self._load_yaml(self.config_path)
            with self.config_lock:
                self.CONFIG = new_config
            print("[CONFIG] 配置已重新加载")
        except Exception as e:
            print(f"[CONFIG] 加载配置失败: {e}")

    def get_config(self, key: str, default=None):
        """
        安全地从 CONFIG 中读取配置项。
        支持点号路径，也兼容原来的一级键读取。
        """
        with self.config_lock:
            if "." in key:
                cur = self.CONFIG
                for part in key.split("."):
                    if not isinstance(cur, dict):
                        return default
                    cur = cur.get(part, default)
                return cur
            return self.CONFIG.get(key, default)

    # 监听内部类
    class _ConfigFileEventHandler(FileSystemEventHandler):
        def __init__(self, logger_manager):
            self.logger_manager = logger_manager

        def on_modified(self, event):
            if os.path.abspath(event.src_path) == os.path.abspath(self.logger_manager.config_path):
                print("[CONFIG] 检测到配置文件修改，重新加载配置…")
                self.logger_manager.reload_config()

    def start_config_watcher(self) -> None:
        """
        启动文件监听，实现配置文件的实时热更新。
        不需要热更新时，可以完全不调用本函数。
        """
        if not self.CONFIG:  # 避免重复调用
            self.reload_config()

        event_handler = self._ConfigFileEventHandler(self)
        self.observer = Observer()
        self.observer.daemon = True  # 主线程退出时自动关闭
        self.observer.schedule(event_handler, path=os.path.dirname(self.config_path), recursive=False)
        self.observer.start()
        print("[CONFIG] 文件监听器已启动")

    def stop_config_watcher(self) -> None:
        """停止文件监听器。"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            print("[CONFIG] 文件监听器已停止")

    # 设置日志的显示格式
    def setup_logger(self, name='MQI', log_folder='test-logs', log_level="DEBUG", to_console=True):
        """
        配置并返回一个日志记录器，支持将调试日志与错误日志分别输出到不同文件夹中，
        每天轮转日志文件，保留 30 天，同时可选是否输出到控制台。

        :param name: 日志记录器名称。
        :param log_folder: 日志根目录。
        :param log_level: 控制台日志等级，支持字符串（如 "INFO"）或 logging 模块常量（如 logging.INFO）。
        :param to_console: 是否将日志输出到控制台（默认开启）。
        :return: 配置完成的 logger 实例。
        """
        BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        
        # 解析控制台日志等级
        if isinstance(log_level, str):
            console_level = logging._nameToLevel.get(log_level.upper(), logging.INFO)
        else:
            console_level = log_level

        # 将相对路径转为绝对路径
        log_folder = (
            log_folder if os.path.isabs(log_folder)
            else os.path.join(BASE_DIR, log_folder)
        )

        # 日志文件夹路径
        debug_log_folder = os.path.join(log_folder, 'debug')
        error_log_folder = os.path.join(log_folder, 'error')
        os.makedirs(debug_log_folder, exist_ok=True)
        os.makedirs(error_log_folder, exist_ok=True)

        # 获取或创建 logger
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)  # 捕获所有级别的日志
        logger.propagate = False  # 禁用传播到父记录器

        # Debug 文件日志处理器（每天轮转，保留 30 天）
        debug_handler = TimedRotatingFileHandler(
            os.path.join(debug_log_folder, 'debug.log'),
            when='midnight', interval=1, backupCount=30, encoding='utf-8'
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))

        # Error 文件日志处理器（每天轮转，保留 30 天）
        error_handler = TimedRotatingFileHandler(
            os.path.join(error_log_folder, 'error.log'),
            when='midnight', interval=1, backupCount=30, encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d - %(funcName)s()] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))

        # 控制台输出处理器（可选）
        if to_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(console_level)
        #     console_handler.setFormatter(logging.Formatter(
        #     '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d - %(funcName)s()] - %(message)s',
        #     datefmt='%Y-%m-%d %H:%M:%S'
        # ))
            # 创建彩色控制台处理器
            console_formatter = coloredlogs.ColoredFormatter(
                '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d - %(funcName)s()] - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S',
                level_styles={
                    'debug': {'color': 'cyan'},
                    # 'info': {'color': 'green'},
                    'info': {'color': 'white'},
                    'warning': {'color': 'yellow'},
                    'error': {'color': 'red', 'bold': True},
                    'critical': {'color': 'magenta', 'bold': True}
                },
                field_styles={
                    'asctime': {'color': 'blue'},
                    'levelname': {'color': 'black', 'bold': True},
                    'filename': {'color': 'blue'},
                    'lineno': {'color': 'blue'}
                }
            )
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)

        # 添加文件处理器
        logger.addHandler(debug_handler)
        logger.addHandler(error_handler)

        # 2. SQLAlchemy logger
        for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool"):
            sa = logging.getLogger(name)
            sa.setLevel(logging.INFO)                # 想存多少就调多少
            sa.addHandler(debug_handler)                    # 同一个 handler 直接复用
            sa.addHandler(error_handler)
            sa.propagate = False       

        return logger

# 初始化日志管理器
def initialize_logger(logger_manager: LoggerManager, default_log_name="TEST"):
    """
    初始化日志记录器，优先使用配置文件中的 log_config，若失败则使用默认值。
    Args:
        logger_manager: 负责管理日志配置和创建的管理器对象，需提供 get_config 和 setup_logger 方法。
        default_log_name (str): 当配置加载失败时使用的默认日志名称。
    Returns:
        logging.Logger: 配置好的日志记录器实例。
    """
    # 获取日志配置
    config_log = logger_manager.get_config('log_config')

    # 检查配置是否加载成功且为字典
    if config_log is None or not isinstance(config_log, dict):
        print(f"Failed to load 'log_config'. Using default logger name: {default_log_name}")
        log_name = default_log_name
        log_level = "DEBUG"
        log_folder = "test-logs"

        # 仅在没有处理器的情况下初始化
        logger = logging.getLogger(log_name)
        if not logger.handlers:
            logger_manager.setup_logger(name=log_name, log_level=log_level, log_folder=log_folder)
    else:           # 获取 config_log. yml文件配置
        # 使用配置中的值，提供默认 fallback
        log_name = config_log.get('name', "default_logger")
        log_level = config_log.get('log_level', "DEBUG")
        log_folder = config_log.get('log_folder', "test-logs")
        logger_manager.setup_logger(name=log_name, log_level=log_level, log_folder=log_folder)

    # 返回配置好的 logger
    return logging.getLogger(log_name)

# 只加载一次
"""
1. 配置config.yml文件路径地址
2. 设置logger.日志的打印样式    setup_logger
"""
current_file_path = os.path.abspath(__file__)
current_directory = os.path.dirname(current_file_path)
config_path = os.path.join(current_directory, 'config.yml')     # logger_utils/config.yml'

# config_path = "yaml/logger.yml"
logger_manager = LoggerManager(config_path)         # 这里已经调用了 logger_manager.reload_config() 方法 
# logger_manager.reload_config()
logger = initialize_logger(logger_manager)

# 使用示例
if __name__ == "__main__":
    logger.info("你好，中国")
    logger.warning("你好，美国")
    logger.error("你好，日本")
    # logger_manager.start_config_watcher()
    # # pass/
    # # # 示例：获取配置项
    # db_host = logger_manager.get_config("database.host", "127.0.0.1")
    # print(f"Database host: {db_host}")

    # # 确保主线程不会立即退出
    # try:
    #     while True:
    #         pass
    # except KeyboardInterrupt:
    #     logger_manager.stop_config_watcher()