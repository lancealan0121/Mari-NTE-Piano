
from src.char.BaseChar import BaseChar


class Sakiri(BaseChar):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def do_perform(self):
        self.wait_intro()
        self.click_ultimate()
        self.click_skill(down_time=0.25)
