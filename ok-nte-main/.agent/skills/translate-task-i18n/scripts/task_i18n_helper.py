import argparse
import ast
import os

import polib


class TaskStringVisitor(ast.NodeVisitor):
    def __init__(self):
        self.strings = []

    def visit_Assign(self, node):
        for target in node.targets:
            if self._is_self_attr(target, "name") or self._is_self_attr(target, "description"):
                self._add_string(node.value)
        self.generic_visit(node)

    def visit_Call(self, node):
        attr = node.func
        if isinstance(attr, ast.Attribute) and attr.attr == "update":
            if self._is_self_attr(attr.value, "default_config") or self._is_self_attr(
                attr.value, "config_description"
            ):
                for arg in node.args:
                    self._collect_dict_strings(arg)
        self.generic_visit(node)

    def _is_self_attr(self, node, attr):
        return (
            isinstance(node, ast.Attribute)
            and node.attr == attr
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    def _add_string(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self.strings.append(node.value)

    def _collect_dict_strings(self, node):
        if not isinstance(node, ast.Dict):
            return
        for key, value in zip(node.keys, node.values):
            self._add_string(key)
            self._collect_value_strings(value)

    def _collect_value_strings(self, node):
        self._add_string(node)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for item in node.elts:
                self._collect_value_strings(item)
        elif isinstance(node, ast.Dict):
            self._collect_dict_strings(node)


def scan_task(path):
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    visitor = TaskStringVisitor()
    visitor.visit(tree)
    for value in dict.fromkeys(visitor.strings):
        print(value)


def compile_i18n(i18n_dir):
    for root, _, files in os.walk(i18n_dir):
        if "ok.po" not in files:
            continue
        po_path = os.path.join(root, "ok.po")
        mo_path = os.path.join(root, "ok.mo")
        po = polib.pofile(str(po_path))
        po.save_as_mofile(mo_path)
        print(f"compiled {po_path} -> {mo_path}")


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan")
    scan.add_argument("--task", required=True)
    compile_cmd = subparsers.add_parser("compile")
    compile_cmd.add_argument("--i18n", default="i18n")
    args = parser.parse_args()

    if args.command == "scan":
        scan_task(args.task)
    elif args.command == "compile":
        compile_i18n(args.i18n)


if __name__ == "__main__":
    main()
