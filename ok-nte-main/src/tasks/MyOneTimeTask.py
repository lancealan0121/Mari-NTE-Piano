import re

from qfluentwidgets import FluentIcon

from src.tasks.BaseNTETask import BaseNTETask

INST = r"""
        <span style="color:red;">
            <strong>本软件是免费开源的。</strong> 如果你被收费，请立即退款。请访问 QQ 频道或 GitHub 下载最新的官方版本。<br>
            <strong>This software is free and open-source.</strong> If you were charged for it, please request a refund immediately. Visit the QQ channel or GitHub to download the latest official version.
        </span>
        <span style="color:red;">
            <strong>本软件仅供个人使用，用于学习 Python 编程、计算机视觉、UI 自动化等。</strong> 请勿将其用于任何营利性或商业用途。<br>
            <strong>This software is for personal use only, intended for learning Python programming, computer vision, UI automation, and similar purposes.</strong> Do not use it for any commercial or profit-seeking activities.
        </span>
        <span style="color:red;">
            <strong>使用本软件可能会导致账号被封。</strong> 请在了解风险后再使用。<br>
            <strong>Using this software may result in account bans.</strong> Please proceed only if you fully understand the risks.
        </span>
    """


class MyOneTimeTask(BaseNTETask):
    # --- 配置项键名 ---
    CONF_DROP_DOWN = "下拉菜单选项"
    CONF_BOOL = "是否选项默认支持"
    CONF_INT = "int选项"
    CONF_TEXT = "文字框选项"
    CONF_LONG_TEXT = "长文字框选项"
    CONF_LIST = "list选项"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "点击触发运行任务"
        self.description = "用户点击时调用run方法"
        self.instructions = INST
        self.icon = FluentIcon.SYNC
        self.support_schedule_task = True
        self.default_config.update(
            {
                self.CONF_DROP_DOWN: "第一",
                self.CONF_BOOL: False,
                self.CONF_INT: 1,
                self.CONF_TEXT: "默认文字",
                self.CONF_LONG_TEXT: "默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字默认文字",
                self.CONF_LIST: ["第一", "第二", "第3"],
            }
        )
        self.config_type[self.CONF_DROP_DOWN] = {
            "type": "drop_down",
            "options": ["第一", "第二", "第3"],
        }

    def run(self):
        self.log_info("日常任务开始运行!")
        self.sleep(1)
        self.click(0.47, 0.60)
        self.sleep(1)
        self.run_for_5()
        self.log_info("日常任务运行完成!")

    def find_some_text_on_bottom_right(self):
        return self.ocr(box="bottom_right", match="商城", log=True)  # 指定box以提高ocr速度

    def find_some_text_with_relative_box(self):
        return self.ocr(0.5, 0.5, 1, 1, match=re.compile("招"), log=True)  # 指定box以提高ocr速度

    def test_find_one_feature(self):
        return self.find_one("box_battle_1")

    def test_find_feature_list(self):
        return self.find_feature("box_battle_1")

    def run_for_5(self):
        self.send_key_down("w")
        self.sleep(0.1)
        self.mouse_down(key="right")
        self.sleep(0.1)
        self.mouse_up(key="right")
        self.sleep(5)
        self.send_key_up("w")
