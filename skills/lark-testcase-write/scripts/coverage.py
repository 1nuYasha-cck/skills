"""Mechanical coverage of the Agent supplied model; no completeness inference."""
def require(condition, message):
    if not condition:
        raise ValueError(message)


def leaves(node):
    if isinstance(node, str):
        return {node}
    require(isinstance(node, dict) and len(node) == 1, 'Expected a restricted Boolean tree')
    op, children = next(iter(node.items()))
    require(op in {'and', 'or', 'not'} and isinstance(children, list) and children, 'Unsupported Boolean node')
    require(op != 'not' or len(children) == 1, 'not takes one child')
    return set().union(*(leaves(c) for c in children))


def evaluate(node, values):
    if isinstance(node, str):
        return values[node]
    op, children = next(iter(node.items()))
    results = [evaluate(c, values) for c in children]
    return all(results) if op == 'and' else any(results) if op == 'or' else not results[0]


def compute_mcdc(decision):
    conditions = decision['conditions']
    require(conditions and len(set(conditions)) == len(conditions), 'Duplicate or empty conditions')
    require(set(conditions) == leaves(decision['expression']), 'Condition set differs from expression')
    obs = decision.get('observations', [])
    for o in obs:
        require(set(o['values']) == set(conditions) and all(type(v) is bool for v in o['values'].values()), 'Expected all Boolean values')
        require(o.get('case'), 'Observation needs a case')
    pairs = {}
    for target in conditions:
        for i, a in enumerate(obs):
            for b in obs[i + 1:]:
                if (a['case'] != b['case'] and a.get('context') == b.get('context')
                    and a['values'][target] != b['values'][target]
                    and all(a['values'][k] == b['values'][k] for k in conditions if k != target)
                    and evaluate(decision['expression'], a['values']) != evaluate(decision['expression'], b['values'])):
                    pairs.setdefault(target, [a['case'], b['case']])
    return {'decision_id': decision['decision_id'], 'pairs': pairs,
            'missing': [c for c in conditions if c not in pairs],
            'total': len(conditions), 'covered': len(pairs), 'rate': len(pairs) / len(conditions)}


def compute_coverage(doc):
    cases = doc['cases']
    ids = [c['用例编号'] for c in cases]
    require(len(ids) == len(set(ids)), 'Duplicate case IDs')
    reqs = [r['id'] for r in doc.get('requirements', [])]
    items = doc.get('coverage', {}).get('items', [])
    item_ids = [i['id'] for i in items]
    require(len(reqs) == len(set(reqs)) and len(item_ids) == len(set(item_ids)), 'Duplicate model IDs')
    counts = dict.fromkeys(reqs, 0)
    icounts = dict.fromkeys(item_ids, 0)
    for c in cases:
        for key, known in [('需求编号', counts), ('覆盖项', icounts)]:
            refs = c.get(key, [])
            require(isinstance(refs, list), key + ' must be an array')
            for ref in set(refs):
                require(ref in known, 'Unknown reference: ' + ref)
                known[ref] += 1
    for item in items:
        require(item.get('requirement') in counts, 'Unknown coverage item requirement')
    decisions = doc.get('coverage', {}).get('mcdc', [])
    require(len({d['decision_id'] for d in decisions}) == len(decisions), 'Duplicate decisions')
    for d in decisions:
        require(d.get('requirement') in counts, 'Unknown decision requirement')
        for o in d.get('observations', []):
            require(o.get('case') in ids, 'Unknown observation case')
            c = cases[ids.index(o['case'])]
            require(d['requirement'] in c.get('需求编号', []), 'Observation case does not cover decision requirement')
    mcdc = [compute_mcdc(d) for d in decisions]
    total = sum(d['total'] for d in mcdc)
    rates = {'requirement': sum(v > 0 for v in counts.values()) / len(counts) if counts else None,
             'item': sum(v > 0 for v in icounts.values()) / len(icounts) if icounts else None,
             'mcdc': sum(d['covered'] for d in mcdc) / total if total else None}
    methods = {}
    for method in {i['method'] for i in items}:
        group = [i for i in items if i['method'] == method]
        methods[method] = sum(icounts[i['id']] > 0 for i in group) / len(group)
        rates[method] = methods[method]
    targets = doc.get('design_requirements', {}).get('targets', {})
    for key, value in targets.items():
        require(isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1, 'Target must be between 0 and 1')
    reached = {k: rates.get(k) is not None and rates[k] >= v for k, v in targets.items()}
    return {'rates': rates, 'requirements': counts, 'missing_requirements': [k for k,v in counts.items() if not v],
            'items': icounts, 'missing_items': [k for k,v in icounts.items() if not v],
            'methods': methods, 'mcdc': mcdc, 'targets_reached': reached,
            'achieved': all(reached.values()), 'boundary': 'Agent supplied model only; model completeness and test execution are not verified'}


def coverage_markdown(doc, report):
    """Human-readable accounting; semantics still belong to the supplied model."""
    def escape(value):
        return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\n', '<br>')
    def rate(value):
        return '无模型/不适用' if value is None else f'{value:.1%}'
    def table(headers, rows):
        lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |']
        lines += ['| ' + ' | '.join(escape(v) for v in row) + ' |' for row in rows]
        if not rows:
            lines += ['| ' + ' | '.join(['无记录'] + ['—'] * (len(headers)-1)) + ' |']
        return '\n'.join(lines)
    targets = doc.get('design_requirements', {}).get('targets', {})
    scopes = list(dict.fromkeys([*report['rates'], *targets]))
    unmodeled_targets = [key for key in targets if report['rates'].get(key) is None]
    lines = ['# 覆盖报告', '', '仅核算 Agent 提供的模型；不证明模型完整、需求判断正确或测试已执行。', '',
             '## 覆盖与目标', '', table(['范围', '覆盖率', '目标', '结论'],
             [[key, rate(report['rates'].get(key)), rate(targets[key]) if key in targets else '未指定',
               ('达到' if report['targets_reached'][key] else '未达到') if key in targets else '未指定目标']
              for key in scopes]), '', '## 需求覆盖与缺口', '',
             table(['需求编号', '用例数', '状态'], [[key,n,'已覆盖' if n else '缺口'] for key,n in report['requirements'].items()]), '',
             '## 覆盖项（按设计方法）', '']
    items = doc.get('coverage', {}).get('items', [])
    for method in sorted({item['method'] for item in items}):
        group = [item for item in items if item['method'] == method]
        lines += ['### ' + escape(method), '', '覆盖率：' + rate(report['methods'][method]), '',
                  table(['覆盖项', '需求', '说明', '用例数', '状态'],
                        [[item['id'],item['requirement'],item.get('description',''),report['items'][item['id']],
                          '已覆盖' if report['items'][item['id']] else '缺口'] for item in group]), '']
    if not items:
        lines += ['未提供覆盖项模型。', '']
    lines += ['## MC/DC 独立影响对与缺口', '']
    definitions = {d['decision_id']:d for d in doc.get('coverage', {}).get('mcdc', [])}
    rows = []
    for decision in report['mcdc']:
        definition = definitions[decision['decision_id']]
        for condition in definition['conditions']:
            pair = decision['pairs'].get(condition)
            rows.append([decision['decision_id'],definition['requirement'],condition,
                         ' ↔ '.join(pair) if pair else '—','已覆盖' if pair else '缺口'])
    lines += [table(['判定', '需求', '条件', '独立影响对用例', '状态'], rows), '',
              '## 未覆盖汇总', '',
              '- 需求：' + (', '.join(escape(v) for v in report['missing_requirements']) or '无'),
              '- 覆盖项：' + (', '.join(escape(v) for v in report['missing_items']) or '无'),
              '- MC/DC 条件：' + (', '.join(escape(d['decision_id'] + '/' + c) for d in report['mcdc'] for c in d['missing']) or ('无' if report['mcdc'] else '未提供模型')),
              '- 未建模的目标：' + (', '.join(escape(key) for key in unmodeled_targets) or '无'),
              '', '目标结论：' + ('全部达到' if report['achieved'] else '存在未达到目标') if targets else '目标结论：未指定目标。', '']
    return '\n'.join(lines)
