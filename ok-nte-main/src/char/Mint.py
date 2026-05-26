
from src.char.BaseChar import BaseChar


class Mint(BaseChar):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def do_perform(self):
        self.wait_intro()
        self.click_ultimate()
        self.click_skill()
