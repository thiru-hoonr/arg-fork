"""Classes found by the tree-sitter module.

module_classes(root, file, lang) -> rows: file, name, line_start, line_end, kind, nest
Languages: java, javascript (incl. jsx), python. Anonymous classes are not listed (no name), as in the references.

Rules:
 1 (java)   annotation types (@interface) are classes. CRG makes no Class node for them
            (guava: 140 of 7,400 missed; notebooks/01-five-projects/class-check-guava.ipynb).
 2 (js)     a class expression takes its own name, else the variable/property it is assigned to.
 3 (python) a decorated class starts at its first decorator.
"""
from pathlib import Path
from tree_sitter_language_pack import get_parser

KINDS = {
    'java': {'class_declaration': 'class', 'interface_declaration': 'interface', 'enum_declaration': 'enum',
             'record_declaration': 'record', 'annotation_type_declaration': 'annotation'},   # rule 1 adds the last one
    'javascript': {'class_declaration': 'class', 'class': 'class_expression'},
    'python': {'class_definition': 'class'},
}


ALL_RULES = frozenset({1, 2, 3})
RULES = {1: 'java: annotation types (@interface) are classes',
         2: 'js: class expression named by the variable/property it is assigned to',
         3: 'python: decorated class starts at its first decorator'}


def _name(node, lang, rules):
    n = node.child_by_field_name('name')
    if n is not None:
        return n.text.decode()
    p = node.parent  # rule 2
    if 2 in rules and lang == 'javascript' and p is not None:
        if p.type in ('variable_declarator', 'pair'):
            k = p.child_by_field_name('name') or p.child_by_field_name('key')
            if k is not None and k.type in ('identifier', 'property_identifier'):
                return k.text.decode()
        if p.type == 'assignment_expression':
            left = p.child_by_field_name('left')
            if left is not None and left.type == 'identifier':
                return left.text.decode()
            if left is not None and left.type == 'member_expression':
                return left.child_by_field_name('property').text.decode()
    return ''


def module_classes(root, file, lang='java', rules=ALL_RULES):
    """rules: the rule numbers switched on (default all); used by the notebooks to prove what each rule fixes."""
    kinds = dict(KINDS[lang])
    if 1 not in rules:
        kinds.pop('annotation_type_declaration', None)
    src = (Path(root) / file).read_bytes()
    rows, todo = [], [(get_parser(lang).parse(src).root_node, 0)]
    while todo:
        node, nest = todo.pop()
        kind = kinds.get(node.type)
        if kind:
            name = _name(node, lang, rules)
            start = node.parent if 3 in rules and lang == 'python' and node.parent.type == 'decorated_definition' else node  # rule 3
            if name:
                rows.append({'file': file, 'name': name, 'line_start': start.start_point[0] + 1,
                             'line_end': node.end_point[0] + 1, 'kind': kind, 'nest': nest})
        todo.extend((c, nest + (1 if kind else 0)) for c in reversed(node.children))
    return rows
