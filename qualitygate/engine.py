"""Pure rule evaluation. No database, HTTP, network, or user code execution."""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import re
import operator
from typing import Callable

from .errors import AppError

TYPES = ("required", "unique", "numeric_range", "enum", "date", "email", "compare_fields", "reference")
EMAIL = re.compile(r"^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+$")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
COMPARISONS={"gte":operator.ge,"gt":operator.gt,"eq":operator.eq,"lte":operator.le,"lt":operator.lt}


def number(value):
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("不是有效数字") from None
    if not result.is_finite():
        raise ValueError("数字必须为有限值")
    return result


def iso_date(value):
    if not ISO_DATE.fullmatch(value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    return date.fromisoformat(value)


def validate_rules(rules):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 32:
        raise AppError("规则数量必须为 1 至 32 条")
    seen = set()
    normalized = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise AppError("每条规则必须为对象")
        rid = rule.get("id", f"R{index + 1:02}")
        kind = rule.get("type")
        column = rule.get("column")
        severity = rule.get("severity", "error")
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", rid) or rid in seen:
            raise AppError("规则 ID 必须唯一，限32位字母、数字、下划线或短横线")
        if kind not in TYPES or severity not in ("error", "warning"):
            raise AppError("不支持的规则类型或严重级别")
        if not isinstance(column, str) or not column.strip() or len(column) > 120:
            raise AppError("规则必须指定有效列名")
        params = rule.get("params", {})
        if not isinstance(params, dict):
            raise AppError("规则参数必须为对象")
        clean = {}
        if kind == "numeric_range":
            for key in ("min", "max"):
                if key in params and params[key] is not None:
                    try:
                        clean[key] = str(number(params[key]))
                    except ValueError as exc:
                        raise AppError(f"数值范围参数错误：{exc}") from None
            if not clean or ("min" in clean and "max" in clean and number(clean["min"]) > number(clean["max"])):
                raise AppError("至少设置一个有效边界，且最小值不能大于最大值")
        elif kind == "enum":
            values = params.get("values")
            if not isinstance(values, list) or not 1 <= len(values) <= 100 or any(not isinstance(v, str) or len(v) > 500 for v in values):
                raise AppError("枚举 values 必须为1至100个字符串")
            clean["values"] = list(dict.fromkeys(v.strip() for v in values))
        elif kind == "compare_fields":
            other = params.get("other")
            mode = params.get("mode", "number")
            op = params.get("op", "gte")
            if not isinstance(other, str) or not other or len(other) > 120 or mode not in ("number", "date") or op not in ("gte", "gt", "eq", "lte", "lt"):
                raise AppError("跨字段参数需要 other、mode(number/date)、op(gte/gt/eq/lte/lt)")
            clean = {"other": other, "mode": mode, "op": op}
        elif kind == "reference":
            dataset_id, ref_column = params.get("dataset_id"), params.get("column")
            if not isinstance(dataset_id, str) or not re.fullmatch(r"[a-f0-9]{32}", dataset_id) or not isinstance(ref_column, str) or not ref_column or len(ref_column) > 120:
                raise AppError("引用规则需要有效的参考数据集 ID 和参考列")
            clean = {"dataset_id": dataset_id, "column": ref_column}
        name = rule.get("name", f"{column} · {kind}")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise AppError("规则名称必须为1至120个字符")
        normalized.append({"id": rid, "type": kind, "column": column, "severity": severity, "name": name.strip(), "params": clean})
        seen.add(rid)
    return normalized


class Cancelled(Exception):
    pass


def evaluate(rows: list[dict], rules: list[dict], references: dict, max_issues=10000,
             checkpoint: Callable[[int], bool] = lambda progress: False):
    """Each (rule, row) yields passed / failed / skipped. Count all; retain bounded detail.

    CSV row numbers count logical records, starting at 2 after the header. Non-required
    rules skip blank cells; pair comparisons also skip blank counterpart cells.
    """
    issues, results, bad_rows, error_rows = [], [], set(), set()
    errors = warnings = passed = skipped = 0
    total = len(rows) * len(rules)
    done = 0
    for rule in rules:
        col, kind, params = rule["column"], rule["type"], rule["params"]
        counts = Counter(row[col].strip() for row in rows if row[col].strip()) if kind == "unique" else {}
        allowed = set(params.get("values", [])) if kind == "enum" else set()
        ref_values = set(references.get(rule["id"], {}).get("values", []))
        minimum=number(params["min"]) if kind=="numeric_range" and "min" in params else None
        maximum=number(params["max"]) if kind=="numeric_range" and "max" in params else None
        compare=COMPARISONS[params["op"]] if kind=="compare_fields" else None
        converter=iso_date if params.get("mode")=="date" else number
        stats = {"id": rule["id"], "name": rule["name"], "type": kind, "column": col,
                 "severity": rule["severity"], "passed": 0, "failed": 0, "skipped": 0}
        for offset, row in enumerate(rows):
            if done % 128 == 0 and checkpoint(int(done * 100 / max(total, 1))):
                raise Cancelled()
            done += 1
            value = row[col].strip()
            other_value = row[params["other"]].strip() if kind == "compare_fields" else None
            if kind != "required" and (not value or (kind == "compare_fields" and not other_value)):
                stats["skipped"] += 1
                skipped += 1
                continue
            reason = ""
            try:
                if kind == "required" and not value:
                    reason = "必填值为空"
                elif kind == "unique" and counts[value] > 1:
                    reason = f"重复值，共出现 {counts[value]} 次（标记全部重复行）"
                elif kind == "numeric_range":
                    numeric = number(value)
                    if minimum is not None and numeric < minimum:
                        reason = f"小于最小值 {params['min']}"
                    elif maximum is not None and numeric > maximum:
                        reason = f"大于最大值 {params['max']}"
                elif kind == "enum" and value not in allowed:
                    reason = "不在允许值集合中"
                elif kind == "date":
                    iso_date(value)
                elif kind == "email" and (len(value) > 254 or not EMAIL.fullmatch(value)):
                    reason = "不符合基础邮箱格式（不验证邮箱是否存在）"
                elif kind == "compare_fields":
                    left, right = converter(value), converter(other_value)
                    valid = compare(left,right)
                    if not valid:
                        reason = f"与 {params['other']} 的 {params['op']} 比较未通过"
                elif kind == "reference" and value not in ref_values:
                    reason = "参考数据集中不存在此值"
            except (ValueError, InvalidOperation) as exc:
                reason = str(exc) or "格式无效"
            if reason:
                stats["failed"] += 1
                row_number = offset + 2
                bad_rows.add(row_number)
                if rule["severity"] == "error":
                    errors += 1
                    error_rows.add(row_number)
                else:
                    warnings += 1
                if len(issues) < max_issues:
                    issues.append({"row_number": row_number, "rule_id": rule["id"], "column": col,
                                   "severity": rule["severity"], "value": row[col], "message": reason})
            else:
                stats["passed"] += 1
                passed += 1
        results.append(stats)
    if checkpoint(100):
        raise Cancelled()
    evaluated = passed + errors + warnings
    return {"summary": {"row_count": len(rows), "rule_count": len(rules), "passed": passed,
                        "failed": errors + warnings, "skipped": skipped, "evaluated": evaluated,
                        "error_count": errors, "warning_count": warnings, "bad_rows": len(bad_rows),
                        "error_rows": len(error_rows), "score": round(passed * 100 / evaluated, 2) if evaluated else None,
                        "gate": "blocked" if errors else "review" if warnings else "passed",
                        "stored_issues": len(issues), "truncated": errors + warnings > len(issues)},
            "rules": results, "issues": issues}
