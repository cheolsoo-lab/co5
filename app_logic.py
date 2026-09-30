"""
app_logic.py — 화면(app.py)에서 쓰는 순수 로직 모음 (Streamlit 의존 없음 → 단독 테스트 가능)
카드 HTML 생성, 가격 포맷, 레버리지 상한 가이드, 주문 메모 텍스트 등.
"""
import html
import math
from typing import Dict, Optional, Tuple

REGIME_INFO = {
    "uptrend": {"emoji": "🟢", "title": "상승장", "color": "#16a34a", "bg": "rgba(22,163,74,.13)",
                "desc": "롱 위주. 급등 추격 말고 눌림목(되돌림 지정가)에서 진입"},
    "downtrend": {"emoji": "🔴", "title": "하락장", "color": "#dc2626", "bg": "rgba(220,38,38,.13)",
                  "desc": "숏 위주. 급락 추격 말고 반등 구간에서 진입"},
    "sideways": {"emoji": "🟡", "title": "횡보장", "color": "#d97706", "bg": "rgba(217,119,6,.13)",
                 "desc": "박스 경계에서만 진입. 방향은 기울기 점수로 판단하고 중간 구간은 관망"},
}
TREND_ICON = {"uptrend": "↑", "downtrend": "↓", "sideways": "→"}
TF_LABEL = {"4h": "4시간봉", "1h": "1시간봉"}

# bias → (방향, 한글 방향, 설명)
BIAS_INFO = {
    "long": ("long", "롱", "추세 눌림목 진입"),
    "short": ("short", "숏", "추세 반등 매도"),
    "wait_breakout_long": ("long", "롱", "박스 상단 돌파 후 리테스트"),
    "wait_breakout_short": ("short", "숏", "박스 하단 이탈 후 리테스트"),
    "range_fade_long": ("long", "롱", "박스 하단 반등(평균회귀)"),
    "range_fade_short": ("short", "숏", "박스 상단 저항(평균회귀)"),
    "donchian_long": ("long", "롱", "신고점 돌파(약 20일) · 시장가"),
    "donchian_short": ("short", "숏", "신저점 이탈(약 20일) · 시장가"),
}

CSS = """
.block-container{padding-top:1.1rem;padding-bottom:3rem;max-width:720px}
.regime{border-radius:16px;padding:14px 16px;margin:6px 0 10px 0;border:1px solid rgba(128,128,128,.25)}
.regime .t{font-size:1.35rem;font-weight:800;line-height:1.3}
.regime .d{font-size:.88rem;opacity:.9;margin-top:8px;line-height:1.5}
.gauge{margin-top:12px}
.gauge-track{position:relative;height:8px;border-radius:999px;
  background:linear-gradient(90deg,#dc2626,#9ca3af,#16a34a)}
.gauge-mark{position:absolute;top:-4px;width:16px;height:16px;border-radius:50%;
  background:#fff;border:3px solid #111827;transform:translateX(-50%);box-shadow:0 1px 3px rgba(0,0,0,.4)}
.gauge-lbl{position:absolute;top:10px;font-size:.68rem;opacity:.65}
.gauge-lbl.left{left:0}.gauge-lbl.right{right:0}
.brk{margin-top:14px;font-size:.82rem;background:rgba(128,128,128,.12);border-radius:10px;padding:7px 10px}
.unver{margin-top:8px;font-size:.74rem;opacity:.65;font-style:italic}
.conf{margin-top:10px;font-size:.78rem;opacity:.85}
.conf .hint{opacity:.7;font-size:.72rem}
.regime .act{margin-top:6px;font-size:.92rem;font-weight:650;line-height:1.45}
.gauge-lbl.mid{left:50%;transform:translateX(-50%)}
.rnote{margin-top:8px;font-size:.8rem}
.tf{display:flex;flex-direction:column;gap:6px}
.tfrow{display:flex;justify-content:space-between;align-items:center;gap:8px;
  background:rgba(128,128,128,.08);border-radius:10px;padding:7px 10px}
.tfl{font-size:.82rem}.tfw{display:block;font-size:.68rem;opacity:.6}
.tfv{font-size:.85rem;text-align:right}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.chip{font-size:.75rem;padding:3px 9px;border-radius:999px;background:rgba(128,128,128,.16)}
.cc{border:1px solid rgba(128,128,128,.28);border-left:5px solid var(--c);border-radius:14px;
    padding:12px 14px;margin:10px 0 4px 0;background:rgba(128,128,128,.06)}
.cc.long{--c:#16a34a}.cc.short{--c:#dc2626}
.cc-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-weight:700;font-size:.78rem;padding:2px 10px;border-radius:999px;color:#fff}
.pill.long{background:#16a34a}.pill.short{background:#dc2626}
.sym{font-size:1.15rem;font-weight:800}
.tag{font-size:.72rem;padding:1px 8px;border-radius:999px;background:rgba(245,158,11,.22)}
.sub{opacity:.75;font-size:.82rem;margin:3px 0 8px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.grid div{background:rgba(128,128,128,.11);border-radius:10px;padding:6px 8px}
.k{display:block;font-size:.68rem;opacity:.7}
.v{display:block;font-weight:650;font-size:.9rem;word-break:break-all}
.v.sl{color:#dc2626}.v.tp{color:#16a34a}
.foot{font-size:.8rem;opacity:.9;margin-top:8px;line-height:1.55}
.warn{margin-top:6px;font-size:.78rem;color:#d97706}
.status{font-size:.82rem;font-weight:650;margin-top:4px}
.plan{margin-top:8px;font-size:.8rem;line-height:1.5;background:rgba(59,130,246,.10);border-radius:10px;padding:6px 9px}
.over{margin-top:6px;font-size:.78rem;color:#dc2626;font-weight:650}
.perp{margin-top:4px;font-size:.78rem;background:rgba(245,158,11,.15);border-radius:8px;padding:4px 8px}
.tag.ct{background:rgba(147,51,234,.18)}
.tag.lab{background:rgba(59,130,246,.16)}
.dead{font-size:.85rem;padding:6px 2px;border-bottom:1px solid rgba(128,128,128,.2)}
.watch{font-size:.86rem;padding:7px 2px;border-bottom:1px solid rgba(128,128,128,.2);line-height:1.5}
.hot{margin-top:3px;font-size:.78rem;color:#dc2626;font-weight:650}
"""


def fmt_price(x: Optional[float]) -> str:
    """가격 크기에 맞춰 소수점 자리수를 자동 조절."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.2f}"
    if ax >= 10:
        return f"{x:,.3f}"
    if ax >= 1:
        return f"{x:.4f}"
    if ax >= 0.01:
        return f"{x:.5f}"
    return f"{x:.8f}"


def fmt_money(x: float) -> str:
    return f"{x:,.2f}" if abs(x) < 1000 else f"{x:,.0f}"


def leverage_guide(entry: float, sl: float, liq_multiple: float = 3.0, cap: int = 10) -> int:
    """격리마진 기준 레버리지 상한 가이드.
    청산까지의 거리(≈1/레버리지)가 손절까지의 거리보다 liq_multiple배 이상 멀도록 잡고, 상한은 cap배.
    (수수료·유지증거금은 무시한 근사치입니다. 레버리지가 높을수록 손절 전에 청산될 위험이 커집니다.)"""
    if not entry:
        return 1
    dist = abs(entry - sl) / entry
    if dist <= 0:
        return 1
    return int(max(1, min(cap, math.floor(1 / (liq_multiple * dist)))))


def side_info(bias: str) -> Tuple[str, str, str]:
    return BIAS_INFO.get(bias, ("long", bias, ""))


def sort_key(setup) -> Tuple[float, float]:
    """정렬: 연구실에서 '우선순위'로 통과한 표시가 많은 신호 먼저 → 알트 지수 대비 상대강도(없으면 BTC 대비).
    롱은 강할수록, 숏은 약할수록 위로."""
    from crypto_market_regime import PRIORITY_TAGS  # 지연 임포트
    pri = sum(t in PRIORITY_TAGS for t in (getattr(setup, "tags", None) or []))
    rs_alt = getattr(setup, "rs_alt", None)
    strength = rs_alt if rs_alt is not None else setup.rs
    return (pri, strength if side_info(setup.bias)[0] == "long" else -strength)


STATUS_INFO = {
    "ready": ("✅", "진입가 근처"), "chase": ("⏳", "추격 구간 · 지정가 대기"),
    "invalid": ("❌", "무효 · 손절선을 먼저 넘음"), "missed": ("⌛", "놓침 · 목표1에 먼저 도달"),
}


def exit_plan_text(setup, scale: float = 1.0) -> str:
    """목표1 분할 익절 → 본전 손절 → 추적 손절 계획을 한 줄로."""
    from crypto_market_regime import PARTIAL_TP_FRACTION, TRAIL_ATR  # 지연 임포트
    side = side_info(setup.bias)[0]
    pct = int(round(PARTIAL_TP_FRACTION * 100))
    anchor = "최고가" if side == "long" else "최저가"
    trail = (f"{TRAIL_ATR:g} ATR(≈{fmt_price(TRAIL_ATR * setup.atr * scale)})"
             if getattr(setup, "atr", 0) else f"{TRAIL_ATR:g} ATR")
    return f"목표1에서 {pct}% 익절 → 남은 물량 손절을 진입가로 → 이후 {anchor}에서 {trail} 되돌리면 정리"


def entry_label(setup) -> str:
    """신고점 돌파·즉시 진입은 시장가, 나머지는 지정가."""
    market = setup.bias.startswith("donchian") or "즉시 진입" in (setup.entry_note or "")
    return "진입(시장가)" if market else "진입(지정가)"


def perp_name(setup) -> str:
    ps = getattr(setup, "perp_symbol", "") or ""
    return (ps.split(":")[0] if ps else setup.symbol).replace("/", "")


def order_memo(setup, sizing: Dict, lev: int) -> str:
    """Bitget 선물 주문용 메모. 1000배 단위로 표기되는 선물은 가격·수량을 선물 기준으로 환산."""
    side, side_kr, sub = side_info(setup.bias)
    m = int(getattr(setup, "perp_mult", 1) or 1)
    lines = [
        f"{perp_name(setup)}  {side_kr} ({sub})",
        f"{entry_label(setup)}: {fmt_price(setup.entry_price * m)}",
        f"손절: {fmt_price(setup.sl * m)}",
        f"목표1: {fmt_price(setup.tp1 * m)}  /  목표2(참고): {fmt_price(setup.tp2 * m)}",
        f"청산: {exit_plan_text(setup, m)}",
        f"수량: {sizing['size'] / m:.4f}  (명목 ${fmt_money(sizing['notional'])})",
        f"레버리지 상한 가이드: {lev}배 이하 (격리)",
    ]
    if m > 1:
        lines.append(f"※ Bitget 선물은 {m:,}배 단위 표기라 가격은 ×{m:,}, 수량은 ÷{m:,} 해서 적었어요")
    return "\n".join(lines)


def watch_line(w: Dict) -> str:
    """돌파 임박 관찰 목록 한 줄. 선물이 1000배 표기면 가격도 선물 기준으로 환산."""
    m = int(w.get("perp_mult", 1) or 1)
    name = (w.get("perp_symbol") or w["symbol"]).split(":")[0].replace("/", "")
    long_side = w["side"] == "long"
    what = "⬆ 상단 돌파 대기 (롱)" if long_side else "⬇ 하단 이탈 대기 (숏)"
    vol = w.get("vol_ratio")
    vol_txt = f" · 거래량 {vol:.1f}배" + (" ↑" if vol and vol >= 1.2 else "") if vol else ""
    hot = ('<div class="hot">🔥 지금 경계를 넘는 중 — 이 봉이 경계 밖에서 마감하면(거래량 1.5배 이상) 추천으로 올라와요</div>'
           if w.get("crossing") else "")
    return (f'<div class="watch"><b>{html.escape(name)}</b> {what}<br>'
            f'<span class="k">경계 {fmt_price(w["trigger"] * m)} · 현재가 {fmt_price(w["price"] * m)} · '
            f'남은 거리 {w["dist_atr"]:.1f} ATR{vol_txt} · 박스 {html.escape(w.get("touches", ""))}</span>{hot}</div>')


def paused_line(setup) -> str:
    """자동 중지된 신호 한 줄 (참고용)."""
    side, side_kr, sub = side_info(setup.bias)
    return (f'<div class="dead"><b>{html.escape(setup.symbol)}</b> {side_kr} · {html.escape(sub)} '
            f'<span class="k">(진입 {fmt_price(setup.entry_price)} · 손절 {fmt_price(setup.sl)})</span></div>')


PORT_ICON = {"신규 진입": "🟢", "비중 조정": "🔄", "정리": "🔴", "유지": "⚪"}


def portfolio_line(r: Dict) -> str:
    """추세 포트폴리오 한 줄. 선물이 1000배 표기면 가격을 선물 기준으로 환산."""
    m = int(r.get("perp_mult", 1) or 1)
    name = (r.get("perp_symbol") or r["symbol"]).split(":")[0].replace("/", "")
    if r["status"] == "정리":
        why = f" — {html.escape(r['reason'])}" if r.get("reason") else ""
        body = f"전량 정리 (어제 비중 {r['prev_weight']:.0%}){why}"
    elif r["status"] == "비중 조정":
        body = f"비중 {r['prev_weight']:.0%} → <b>{r['weight']:.0%}</b> (명목 ${fmt_money(r['notional'])})"
    else:
        body = f"목표 비중 <b>{r['weight']:.0%}</b> (명목 ${fmt_money(r['notional'])})"
    return (f'<div class="watch">{PORT_ICON.get(r["status"], "")} <b>{html.escape(name)}</b> {r["status"]} · {body}<br>'
            f'<span class="k">종가 {fmt_price(r["close"] * m)} · 50일선 {fmt_price(r["exit_line"] * m)}'
            f'{" · 30일 수익률 " + format(r["ret30"], "+.1%") if r.get("ret30") is not None else ""}'
            f'{" · 선물 ×" + format(m, ",") + " 표기" if m > 1 else ""}</span></div>')


def dead_line(setup) -> str:
    """무효·놓침 추천을 한 줄로."""
    side, side_kr, _ = side_info(setup.bias)
    icon, label = STATUS_INFO.get(setup.live_status, ("", setup.live_status))
    return (f'<div class="dead"><b>{html.escape(setup.symbol)}</b> {side_kr} · {icon} {html.escape(label)} '
            f'<span class="k">(현재가 {fmt_price(setup.current_price)})</span></div>')


def card_html(setup, sizing: Dict, risk_pct: float, over_limit: bool = False) -> str:
    side, side_kr, sub = side_info(setup.bias)
    lev = leverage_guide(setup.entry_price, setup.sl)
    e = html.escape
    tags = ""
    if setup.poc_confluence:
        tags += '<span class="tag">🎯 매물대 겹침</span>'
    if setup.sweep_confluence:
        tags += '<span class="tag">🩸 유동성 스윕</span>'
    if getattr(setup, "counter_trend", False):
        tags += '<span class="tag ct">↔ 시장 역행</span>'
    status = getattr(setup, "live_status", "") or ("chase" if setup.is_chase else "ready")
    s_icon, s_label = STATUS_INFO.get(status, ("", ""))
    from crypto_market_regime import TAG_LABELS, PRIORITY_TAGS  # 지연 임포트
    for t in getattr(setup, "tags", None) or []:
        star = "⭐ " if t in PRIORITY_TAGS else ""
        tags += f'<span class="tag lab">{star}{e(TAG_LABELS.get(t, t))}</span>'
    stats = f"BTC 대비 {setup.rs:+.1f}%"
    if getattr(setup, "rs_alt", None) is not None:
        stats += f" · 알트 대비 {setup.rs_alt:+.1f}%"
    warn = ""
    if status == "chase" and setup.entry_price:
        gap = (setup.current_price - setup.entry_price) / setup.entry_price * 100
        warn = (f'<div class="warn">⏳ 현재가가 진입가보다 {abs(gap):.1f}% '
                f'{"위" if gap > 0 else "아래"} — 지정가만 걸어두고 기다리세요 (지금 시장가 진입은 추격)</div>')

    def cell(k: str, v: str, cls: str = "") -> str:
        return f'<div><span class="k">{e(k)}</span><span class="v {cls}">{e(v)}</span></div>'

    grid = "".join([
        cell("현재가", fmt_price(setup.current_price)),
        cell(entry_label(setup), fmt_price(setup.entry_price)),
        cell("손절", fmt_price(setup.sl), "sl"),
        cell("목표1", fmt_price(setup.tp1), "tp"),
        cell("목표2(참고)", fmt_price(setup.tp2), "tp"),
        cell("손익비", f"{setup.rr_ratio:.2f}"),
    ])
    foot = (f"수량 <b>{sizing['size']:.4f}</b> · 명목 ${fmt_money(sizing['notional'])} · "
            f"손절 시 손실 ${fmt_money(sizing['risk_amount'])} (계좌의 {risk_pct:g}%)<br>"
            f"레버리지 상한 가이드 <b>{lev}배 이하</b> (격리마진)")
    plan = f'<div class="plan">🧭 청산: {e(exit_plan_text(setup))}</div>'
    m = int(getattr(setup, "perp_mult", 1) or 1)
    perp_note = (f'<div class="perp">Bitget 선물 표기 <b>{e(perp_name(setup))}</b> — 선물 주문 가격은 아래 값 ×{m:,} '
                 f'(주문 메모에는 환산해서 적었어요)</div>') if m > 1 else ""
    if over_limit:
        warn += '<div class="over">⛔ 총 리스크 상한 초과 — 지금은 참고만 (보유 포지션이 정리되면 검토)</div>'
    if getattr(setup, "health", "") == "caution":
        warn += ('<div class="over">🛡 자동 방어: 이 신호 유형의 최근 실전 성과가 기준 아래라 '
                 '권장 리스크를 절반으로 낮춰 수량을 계산했어요</div>')
    return (f'<div class="cc {side}">'
            f'<div class="cc-head"><span class="pill {side}">{side.upper()} {e(side_kr)}</span>'
            f'<span class="sym">{e(setup.symbol)}</span>{tags}</div>'
            f'<div class="status">{s_icon} {e(s_label)}</div>{perp_note}'
            f'<div class="sub">{e(sub)} · {e(stats)}</div>'
            f'<div class="grid">{grid}</div>'
            f'{plan}<div class="foot">{foot}</div>{warn}</div>')


def regime_html(regime, macro_hours: float = 0.0) -> str:
    """메인 거시 방향 카드: 한 줄 결론 → 행동 가이드 → 방향 게이지 → 근거 설명 → (횡보) 전환 기준선 → 신뢰도."""
    info = REGIME_INFO[regime.overall]
    score = max(-1.0, min(1.0, regime.score))
    pos_pct = (score + 1) / 2 * 100
    conf_color = {"높음": "#16a34a", "보통": "#d97706", "낮음": "#6b7280"}.get(regime.confidence_label, "#6b7280")
    e = html.escape

    breakout = ""
    if regime.breakout_up and regime.breakout_down:
        from crypto_market_regime import fmt_range  # 지연 임포트(app_logic 단독 임포트 가능하게)
        breakout = (f'<div class="brk">⬆ <b>{e(fmt_range(regime.breakout_up))}</b> 돌파 시 상승 전환 · '
                    f'⬇ <b>{e(fmt_range(regime.breakout_down))}</b> 이탈 시 하락 전환</div>')
    notes = ""
    if regime.shock:
        notes += '<div class="rnote">⚡ BTC 급변 감지 — 국면을 즉시 반영했어요</div>'
    if regime.transition_pending:
        notes += '<div class="rnote">⏳ 새 방향이 나왔지만 아직 확인 중 — 다음 봉 마감 때 확정돼요</div>'
    if regime.lean_verified is False:
        notes += '<div class="unver">ⓘ 횡보 중 방향 판단(기울기)은 아직 실측 검증 전이라 참고용이에요</div>'

    return (
        f'<div class="regime" style="background:{info["bg"]};border-left:5px solid {info["color"]}">'
        f'<div class="t">{info["emoji"]} {e(regime.headline)}</div>'
        f'<div class="act">▶ {e(regime.action)}</div>'
        f'<div class="gauge"><div class="gauge-track">'
        f'<span class="gauge-lbl left">하락</span><span class="gauge-lbl mid">중립</span>'
        f'<span class="gauge-lbl right">상승</span>'
        f'<div class="gauge-mark" style="left:{pos_pct:.1f}%"></div></div></div>'
        f'<div class="d">{e(regime.explanation)}</div>'
        f'{breakout}{notes}'
        f'<div class="conf">판단 신뢰도 <b style="color:{conf_color}">{e(regime.confidence_label)}</b>'
        f' <span class="hint">· 근거들이 같은 방향을 가리키는 정도</span></div>'
        f'</div>'
    )


def regime_detail_html(regime) -> str:
    """'판단 근거 자세히' 안에 넣는 근거별 정리 (메인 카드에는 결론만)."""
    e = html.escape
    kr = {"uptrend": "상승", "downtrend": "하락", "sideways": "횡보"}
    color = {"uptrend": "#16a34a", "downtrend": "#dc2626", "sideways": "#d97706"}

    def row(label: str, weight: str, value_html: str) -> str:
        return (f'<div class="tfrow"><span class="tfl">{e(label)}<span class="tfw">{e(weight)}</span></span>'
                f'<span class="tfv">{value_html}</span></div>')

    def trend(t: str) -> str:
        return f'<b style="color:{color[t]}">{TREND_ICON[t]} {kr[t]}</b>'

    rows = [
        row("큰 흐름 · BTC 일봉", "비중 35%", trend(regime.daily_trend)),
        row(f"중기 흐름 · BTC {TF_LABEL.get(getattr(regime, 'timeframe', '4h'), '4시간봉')}", "비중 30%",
            trend(regime.btc_trend)),
    ]
    if regime.breadth_up_pct is not None:
        rows.append(row(f"시장 참여 · 코인 {regime.breadth_n}개", "비중 25%",
                        f'<b style="color:#16a34a">상승 {regime.breadth_up_pct:.0f}%</b> · '
                        f'<b style="color:#dc2626">하락 {regime.breadth_down_pct:.0f}%</b>'))
    else:
        rows.append(row("시장 참여 · 스캔 코인", "비중 25%", "이번엔 스캔 전이라 미반영"))
    span = (regime.snapshot or {}).get("macro_span_hours", 0.0)
    if span >= 24:
        rows.append(row("도미넌스·TOTAL", "비중 10%",
                        f"BTC.D {TREND_ICON[regime.btc_d_trend]} · USDT.D {TREND_ICON[regime.usdt_d_trend]} · "
                        f"TOTAL2 {TREND_ICON[regime.total2_trend]} · TOTAL3 {TREND_ICON[regime.total3_trend]}"))
    else:
        rows.append(row("도미넌스·TOTAL", "비중 10%", f"기록 {span:.0f}시간 — 24시간 이후 반영"))
    rows.append(row("종합 점수", "−1 ~ +1", f"<b>{regime.score:+.2f}</b> (±0.25 넘으면 추세)"))
    return f'<div class="tf">{"".join(rows)}</div>'
