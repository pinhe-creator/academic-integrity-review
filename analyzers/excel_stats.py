"""
数据统计取证 analyzer（Excel / CSV）—— v3
============================================
针对数值数据跑几个【稳健的】取证检验，并把结果【聚合】后呈现。

v3 针对真实案例修了两类问题：

1) 末位数字检验不再被“零值/低精度”误导：
   - 排除精确为 0 的值、排除近常数列（distinct 太少）；
   - 【主导数字为 0 时不报】：float 无法区分 0.4 与 0.40，某位“是 0”往往只是精度/舍入假象，
     没有无辜解释的是“非 0 数字”占多数（如某位几乎全是 7），那才值得报。

2) 等差检验改为【按原始行序】找“近似等差段”：
   - 真实造假常表现为“每行按近乎恒定步长递增/递减”（如 7.222, 7.223, 7.223…），
     而不是排序后才规整；排序会破坏这个信号、又会把稠密取整网格误当等差。
   - 因此在原始顺序上找“差分同号且变异系数极低”的连续段；步长需远大于记录精度。

3) 聚合：每列多项发现合并为一张卡；多列同类发现再跨列合并为一条。

为什么默认不跑 Benford？见 benford_test() 注释。
依赖：pandas, numpy, scipy, openpyxl
"""

from collections import OrderedDict
import numpy as np
import pandas as pd
from scipy import stats
from .base import AnalysisResult

# ---- 末位数字检验 ----
MIN_N_DIGIT = 30          # 末位数字检验所需最少（非零）样本量
MIN_DISTINCT = 10         # 少于这么多不同取值，视为近常数列，不测末位
P_THRESHOLD = 1e-3        # 显著性阈值
MIN_TOP_SHARE = 0.18      # 主导数字占比门槛（均匀为 0.10）

# ---- 近似等差检验（按原始行序）----
MIN_RAMP_RUN = 5          # 近似等差段至少包含的数值个数
CV_TOL = 0.02             # 段内差分的变异系数（std/|mean|）上限
RAMP_STEP_RES_FACTOR = 3.0  # 步长需 > 记录精度 × 此系数（排除稠密取整网格）
MIN_RAMP_DECIMALS = 2     # 段内取值需至少 2 位小数（排除整数型时间/剂量坐标轴）

MAX_GROUPS_PER_TABLE = 50
MAX_COLS_LISTED = 12


def _numeric_columns(df: pd.DataFrame):
    """返回 (列名, 数值序列)。阈值放到 MIN_RAMP_RUN，让短列也能做等差检验；
    末位数字检验在其内部另有更高的样本量要求。"""
    out = []
    for col in df.columns:
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) >= MIN_RAMP_RUN:
            out.append((str(col), s))
    return out


def has_decimals(series: pd.Series) -> int:
    vals = series.values.astype(float)
    if np.all(np.abs(vals - np.round(vals)) < 1e-9):
        return 0
    two = np.abs(vals * 100 - np.round(vals * 100))
    return 2 if np.any(two > 1e-9) else 1


def decimal_places(vals: np.ndarray, max_dp: int = 9) -> int:
    vals = np.abs(vals[np.isfinite(vals)])
    for dp in range(0, max_dp + 1):
        scaled = vals * (10 ** dp)
        if np.all(np.abs(scaled - np.round(scaled)) < 1e-6):
            return dp
    return max_dp


def detect_resolution(series: pd.Series) -> float:
    return 10.0 ** (-decimal_places(series.values.astype(float)))


def terminal_digit_test(series: pd.Series, places: int):
    """小数点后第 `places` 位的数字 vs 均匀分布 的卡方检验（已排除 0 值与近常数列）。"""
    vals = series.values.astype(float)
    vals = vals[np.abs(vals) > 1e-12]                 # 排除精确为 0
    if len(vals) < MIN_N_DIGIT:
        return None
    if len(np.unique(np.round(vals, 9))) < MIN_DISTINCT:  # 近常数列不测
        return None
    digits = (np.floor(np.abs(vals) * (10 ** places)).astype(np.int64)) % 10
    counts = np.bincount(digits, minlength=10)[:10]
    expected = np.full(10, counts.sum() / 10.0)
    chi2, p = stats.chisquare(counts, expected)
    top = int(np.argmax(counts))
    return dict(n=int(counts.sum()), counts=counts.tolist(),
                chi2=round(float(chi2), 2), p=float(p),
                top_digit=top, top_count=int(counts[top]),
                top_share=float(counts[top]) / float(counts.sum()))


def near_arithmetic_run(series: pd.Series):
    """
    在【原始行序】上找最长的“近似等差段”：差分同号、变异系数 < CV_TOL。
    步长需 > 记录精度 × RAMP_STEP_RES_FACTOR（排除稠密取整网格）。
    """
    vals = pd.to_numeric(series, errors="coerce").dropna().values.astype(float)
    if len(vals) < MIN_RAMP_RUN:
        return None
    diffs = np.diff(vals)
    if len(diffs) == 0:
        return None
    res = detect_resolution(series)
    best = None  # (run_len, step, cv, i, j)
    n = len(diffs)
    for i in range(n):
        s0 = np.sign(diffs[i])
        if s0 == 0:
            continue
        for j in range(i + 1, n + 1):
            w = diffs[i:j]
            if np.any(np.sign(w) != s0):
                break
            m = w.mean()
            if m == 0:
                break
            cv = float(w.std() / abs(m))
            if cv > CV_TOL:
                break
            run_vals = (j - i) + 1                     # 段内数值个数
            if abs(m) > res * RAMP_STEP_RES_FACTOR and (best is None or run_vals > best[0]):
                best = (run_vals, float(m), cv, i, j)
    if best is None or best[0] < MIN_RAMP_RUN:
        return None
    # 段内取值需有足够小数精度，否则多半是整数型坐标轴（时间/剂量），排除
    if decimal_places(vals[best[3]: best[4] + 1]) < MIN_RAMP_DECIMALS:
        return None
    return dict(run_len=best[0], step=best[1], cv=best[2])


def benford_test(series: pd.Series):
    """Benford 首位数字检验——仅供手动调用，注意适用条件。"""
    vals = series.values.astype(float)
    vals = np.abs(vals[vals != 0])
    if len(vals) < 50 or vals.min() <= 0:
        return None
    span = np.log10(vals.max()) - np.log10(vals.min())
    if span < 2:
        return dict(applicable=False, reason="数据未跨足够数量级，Benford 不适用")
    first = (vals / (10 ** np.floor(np.log10(vals)))).astype(int)
    counts = np.bincount(first, minlength=10)[1:10]
    expected = np.log10(1 + 1 / np.arange(1, 10)) * counts.sum()
    chi2, p = stats.chisquare(counts, expected)
    return dict(applicable=True, n=int(counts.sum()), chi2=round(float(chi2), 2), p=float(p))


def _cols_label(cols):
    disp = "、".join(cols[:MAX_COLS_LISTED])
    if len(cols) > MAX_COLS_LISTED:
        disp += f" 等共 {len(cols)} 列"
    return disp


def analyze_table(df: pd.DataFrame, sheet_label: str, res: AnalysisResult) -> int:
    n_added = 0

    # --- A. 整行完全重复 ---
    num_df = df.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    if num_df.shape[1] >= 2:
        dup_mask = num_df.duplicated(keep=False) & num_df.notna().any(axis=1)
        n_dup = int(dup_mask.sum())
        if n_dup >= 2:
            idx = [int(i) for i in np.where(dup_mask.values)[0][:12]]
            res.add(
                source="excel_stats", source_label="数据统计取证", kind="stats_anomaly",
                title=f"发现 {n_dup} 行数值完全重复",
                detail=f"在 {sheet_label} 中有 {n_dup} 行的数值内容彼此完全相同"
                       f"（行号约：{idx} …）。整块数据重复在真实实验记录中较少见，建议核对。",
                status="informational", locus=sheet_label,
            )
            n_added += 1

    # --- B. 逐列收集发现 ---
    col_data: "OrderedDict[str, dict]" = OrderedDict()
    for col, s in _numeric_columns(df):
        findings = {}
        d = has_decimals(s)
        if d > 0:
            t = terminal_digit_test(s, places=d)
            # 双重门槛 + 主导数字非 0
            if (t and t["p"] < P_THRESHOLD and t["top_share"] >= MIN_TOP_SHARE
                    and t["top_digit"] != 0):
                findings[("td", d, t["top_digit"])] = t
        r = near_arithmetic_run(s)
        if r:
            findings[("ramp",)] = r
        if findings:
            col_data[col] = findings

    # --- C. 按发现类型组合聚合 ---
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for col, findings in col_data.items():
        groups.setdefault(tuple(sorted(findings.keys())), []).append(col)

    for sigset, cols in groups.items():
        if n_added >= MAX_GROUPS_PER_TABLE:
            break
        parts, title_bits = [], []
        for sig in sigset:
            if sig[0] == "td":
                _, place, digit = sig
                per = "；".join(
                    f"{c}→{100*col_data[c][sig]['top_share']:.0f}%/χ²{col_data[c][sig]['chi2']:.0f}"
                    for c in cols)
                parts.append(f"小数点后第 {place} 位数字「{digit}」显著偏多（均匀期望约 10%）：{per}")
                if "末位数字分布偏斜" not in title_bits:
                    title_bits.append("末位数字分布偏斜")
            elif sig[0] == "ramp":
                per = "；".join(
                    f"{c}→约{col_data[c][sig]['run_len']}个值、步长≈{col_data[c][sig]['step']:.4g}"
                    for c in cols)
                parts.append(f"按原始行序存在近似等差的连续段：{per}")
                if "近似等差序列" not in title_bits:
                    title_bits.append("近似等差序列")

        multi = len(cols) > 1
        title = "、".join(title_bits) + (f"（{len(cols)} 列同样模式）" if multi else "")
        if "近似等差序列" in title_bits:
            tail = ("“每行按近乎恒定步长递增/递减”在真实测量中少见，常见于公式生成的数据，"
                    + ("多列同时如此更" if multi else "") + "值得核对。")
        else:
            tail = ("真实测量的末位数字通常接近均匀；"
                    + ("多列同时出现相同偏斜更" if multi else "此现象") + "值得核对数据生成与记录方式。")
        res.add(
            source="excel_stats", source_label="数据统计取证", kind="stats_anomaly",
            title=title, detail="；\n".join(parts) + "。" + tail,
            status="informational", locus=f"{sheet_label}，列 {_cols_label(cols)}",
        )
        n_added += 1

    return n_added


def analyze_excel(path: str, filename: str) -> AnalysisResult:
    res = AnalysisResult()
    try:
        if filename.lower().endswith(".csv"):
            sheets = {"(CSV)": pd.read_csv(path)}
        else:
            sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except Exception as e:
        res.add(source="excel_stats", source_label="数据统计取证", kind="lookup_skipped",
                title="数据文件读取失败", detail=f"无法解析「{filename}」：{e}",
                status="informational", locus=filename)
        return res

    total = 0
    for sheet_name, df in sheets.items():
        if df is None or df.empty:
            continue
        label = filename if sheet_name == "(CSV)" else f"{filename} · {sheet_name}"
        total += analyze_table(df, label, res)

    if total == 0:
        res.add(source="excel_stats", source_label="数据统计取证", kind="lookup_clean",
                title="未发现明显的数值异常模式",
                detail=f"已对「{filename}」运行末位数字（排除零值/低精度/主导数字为0）、"
                       f"近似等差、重复行检验，未触发预设阈值。仅表示这几项自动检验未发现异常。",
                status="clean", locus=filename)
    return res
