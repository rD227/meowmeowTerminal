"""配置管理模块"""
import os
from typing import Dict, Any, Optional
import yaml
import json
from sys import platform
from path_utils import get_resource_path, ensure_path_exists, get_base_path


class ConfigLoader:
    """配置加载器"""

    def __init__(self):
        self.AUTO_PASTE_IMAGE = True
        self.AUTO_SEND_IMAGE = True
        self.ASSETS_PATH = get_resource_path("assets")

        # 规范化平台键
        self.platform = platform
        if platform.startswith('win'):
            self.platform = 'win32'
        elif platform == 'darwin':
            self.platform = 'darwin'
        else:
            self.platform = 'win32'

        # 加载版本信息
        self.version_info = self._load_yaml_file("version.yml") or {}
        self.version = self.version_info.get("version", "unknown")

        # 配置加载
        self.keymap = self._load_config("keymap")
        self.process_whitelist = self._load_config("process_whitelist")
        self.gui_settings = self._load_config("settings")

    # ------------------------------------------------------------------
    # 内部加载 / 保存
    # ------------------------------------------------------------------

    def _load_config(self, config_type: str) -> Any:
        """通用配置加载"""
        if config_type == "keymap":
            config = self._load_yaml_file("keymap.yml")
            if config:
                return config.get(self.platform, {})
            default = self._get_default_setting("keymap")
            self._save_yaml_file("keymap.yml", {self.platform: default})
            return default

        if config_type == "process_whitelist":
            config = self._load_yaml_file("process_whitelist.yml")
            return config.get(self.platform, []) if config else []

        if config_type == "settings":
            default = self._get_default_setting("settings")
            config = self._load_yaml_file("settings.yml")
            if config:
                return self._deep_merge(default, config)
            self.gui_settings = default
            self.save_gui_settings()
            return default

        raise ValueError(f"不支持的配置类型: {config_type}")

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigLoader._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _get_default_setting(self, config_type: str) -> Dict[str, Any]:
        if config_type == "settings":
            return {
                "cut_settings": {"cut_mode": "全选剪切"},
                "process_whitelist": [],
            }
        if config_type == "keymap":
            if self.platform == "win32":
                return {
                    "toggle_listener": "<ctrl>+<alt>+p",
                }
            return {
                "toggle_listener": "<cmd>+<alt>+p",
            }
        return {}

    def _load_yaml_file(self, filename: str) -> Optional[Dict[str, Any]]:
        filepath = get_resource_path(os.path.join("config", filename))
        if not filepath:
            return None
        try:
            with open(filepath, 'r', encoding="utf-8") as fp:
                return yaml.safe_load(fp) or {}
        except Exception as e:
            print(f"加载配置文件 {filename} 失败: {e}")
            return None

    def _save_yaml_file(self, filename: str, data: Dict[str, Any]) -> bool:
        try:
            # Use base path directly so we can write new files that don't exist yet
            filepath = os.path.join(get_base_path(), "config", filename)
            ensure_path_exists(filepath)
            with open(filepath, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            return True
        except Exception as e:
            print(f"保存配置文件 {filename} 失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 公开保存方法
    # ------------------------------------------------------------------

    def save_keymap(self, new_hotkeys=None):
        if new_hotkeys is None:
            return False
        keymap_data = self._load_yaml_file("keymap.yml") or {self.platform: self._get_default_setting("keymap")}
        current = keymap_data.get(self.platform, {})
        if all(current.get(k) == v for k, v in new_hotkeys.items()):
            return False
        keymap_data.setdefault(self.platform, {}).update(new_hotkeys)
        CONFIGS.keymap = keymap_data[self.platform]
        return self._save_yaml_file("keymap.yml", keymap_data)

    def save_process_whitelist(self, processes):
        existing = self._load_yaml_file("process_whitelist.yml") or {}
        if sorted(processes) == sorted(existing.get(self.platform, [])):
            return False
        existing[self.platform] = processes
        CONFIGS.process_whitelist = processes
        return self._save_yaml_file("process_whitelist.yml", existing)

    def save_gui_settings(self):
        existing = self._load_yaml_file("settings.yml") or {}
        if json.dumps(existing, sort_keys=True) == json.dumps(self.gui_settings, sort_keys=True):
            return False
        existing.update(self.gui_settings)
        return self._save_yaml_file("settings.yml", existing)

    # ------------------------------------------------------------------
    # 版本信息
    # ------------------------------------------------------------------

    def get_program_info(self) -> Dict[str, Any]:
        program = self.version_info.get("program", {})
        return {
            "version": self.version,
            "author": program.get("author", ""),
            "description": program.get("description", ""),
            "github": program.get("github", ""),
        }

    def get_version_history(self) -> list:
        return self.version_info.get("history", [])


CONFIGS = ConfigLoader()
