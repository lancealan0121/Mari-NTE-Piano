import unittest
from src.config import config
from ok.test.TaskTestCase import TaskTestCase
from src.tasks.trigger.AutoCombatTask import AutoCombatTask

config['debug'] = True


class TestCD(TaskTestCase):
    task_class = AutoCombatTask
    config = config

    def test_cd1(self):
        self.task.do_reset_to_false()
        self.set_image('tests/images/01.png')
        self.task.load_chars()
        self.assertTrue(self.task.has_cd('ultimate'))
        self.assertTrue(self.task.has_cd('skill'))

    def test_cd2(self):
        self.task.do_reset_to_false()
        self.set_image('tests/images/02.png')
        self.task.load_chars()
        self.assertFalse(self.task.has_cd('ultimate'))
        self.assertFalse(self.task.has_cd('skill'))

if __name__ == '__main__':
    unittest.main()
