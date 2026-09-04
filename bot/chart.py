"""국면 판독용 차트 이미지 — AI 세션이 사람처럼 보라고 그린다(사용자 결정 2026-09-04).
    python -m bot.chart SYM [SYM...] [--out DIR]

두 패널을 한 장에 쌓는다. **맥락과 매매 스케일은 다른 그림이기 때문이다**: 국면(마크업/분배/클라이맥스/마크다운)은 며칠짜리
판단이라 에피소드의 호(弧)가 보여야 하고, 우리가 실제로 담고 더는 자리는 사이클 보유 중앙 14분의 스케일이다.
  위  = 1시간봉 ~3일 — 에피소드가 어디서 시작해 어디까지 갔는지, 고점이 나왔는지
  아래 = 5분봉 ~10시간 + 거래량 — 지금 몸통이 줄고 있는지, 윗꼬리가 살찌는지, 거래량이 마르는지

발자국 숫자(`whale.footprints`)를 주는 것과 다른 점이 이것이다: 그 숫자는 규칙 판독기가 캔들에서 뽑아낸 특징값이라,
그것만 주면 AI 는 같은 재료를 다시 저울질할 뿐 규칙이 못 보는 것을 볼 수 없다. 이미지는 원래의 모양 그 자체다.

읽기 전용(공개 REST 만). 그린 PNG 경로를 돌려준다."""
import os, sys
import matplotlib
matplotlib.use("Agg")                                     # 헤드리스: 창을 열지 않는다
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.bitget import Bitget

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP, DOWN, GRID, BG, FG = "#26a69a", "#ef5350", "#2a2e39", "#131722", "#d1d4dc"

def _candles(ax, bars, width, vol_ax=None):
    """봉 하나당 몸통 + 심지. x 는 봉 인덱스라 거래가 없는 시간이 빈칸으로 남지 않는다(사람이 보는 차트와 같다)."""
    for i, b in enumerate(bars):
        c = UP if b["c"] >= b["o"] else DOWN
        ax.plot([i, i], [b["l"], b["h"]], color=c, linewidth=0.7, solid_capstyle="butt")
        lo, hi = min(b["o"], b["c"]), max(b["o"], b["c"])
        ax.add_patch(plt.Rectangle((i - width / 2, lo), width, max(hi - lo, (b["h"] - b["l"]) * 1e-3 or 1e-12),
                                   facecolor=c, edgecolor=c, linewidth=0.4))
        if vol_ax is not None: vol_ax.bar(i, b.get("v") or b.get("qv") or 0.0, width=width, color=c, alpha=0.55, linewidth=0)
    ax.set_xlim(-1, len(bars))

def draw(sym, out_dir=None, b=None, ctx=("1H", 72), fine=("5m", 120)):
    """SYM 의 두 패널 차트를 PNG 로 그리고 경로를 돌려준다. 캔들을 못 받으면 None(호출자가 이미지 없이 진행한다)."""
    b = b or Bitget("", "", "")
    try:
        hi = b.candles(sym, ctx[0], limit=ctx[1] + 1)[:-1][-ctx[1]:]      # 마지막 봉은 미완성이라 버린다
        lo = b.candles(sym, fine[0], limit=fine[1] + 1)[:-1][-fine[1]:]
    except Exception:
        return None
    if len(hi) < 10 or len(lo) < 10: return None
    d = out_dir or os.path.join(ROOT, "logs", "ai", "charts")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sym}.png")

    fig = plt.figure(figsize=(9, 7), dpi=110, facecolor=BG)
    gs = fig.add_gridspec(3, 1, height_ratios=[3, 3, 1], hspace=0.16)
    ax1, ax2, ax3 = (fig.add_subplot(g) for g in gs)
    for ax in (ax1, ax2, ax3):
        ax.set_facecolor(BG)
        for s in ax.spines.values(): s.set_color(GRID)
        ax.tick_params(colors=FG, labelsize=7); ax.grid(color=GRID, linewidth=0.4, alpha=0.6)
    fmt = FuncFormatter(lambda v, _: f"{v:.6g}")
    ax1.yaxis.set_major_formatter(fmt); ax2.yaxis.set_major_formatter(fmt)

    _candles(ax1, hi, 0.62)
    hh = max(x["h"] for x in hi); ll = min(x["l"] for x in hi)
    ax1.axhline(hh, color="#787b86", linewidth=0.6, linestyle="--")
    ax1.set_title(f"{sym}   top: {ctx[0]} x{len(hi)} (episode arc)   |   bottom: {fine[0]} x{len(lo)} (trading scale)   "
                  f"high {hh:.6g}  last {lo[-1]['c']:.6g}  ({(lo[-1]['c'] / hh - 1) * 100:+.1f}% off high)",
                  color=FG, fontsize=8.5, pad=6)
    _candles(ax2, lo, 0.62, vol_ax=ax3)
    ax2.axhline(hh, color="#787b86", linewidth=0.5, linestyle="--", alpha=0.7)
    ax3.set_ylabel("vol", color=FG, fontsize=7); ax3.set_xlim(-1, len(lo))
    ax1.set_xticklabels([]); ax2.set_xticklabels([]); ax3.set_yticks([])
    fig.savefig(path, facecolor=BG, bbox_inches="tight"); plt.close(fig)
    return path

def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    b = Bitget("", "", "")
    for sym in a or ["BTCUSDT"]:
        p = draw(sym, out, b)
        print(f"  {sym:14} {p or 'candles unavailable'}" + (f"  {os.path.getsize(p) / 1024:.0f}KB" if p else ""))

if __name__ == "__main__":
    main()
