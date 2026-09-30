"""
app.py — 코인 추천 웹 화면 (핸드폰 우선)
실행:  streamlit run app.py
배포:  README.md 참고 (Streamlit Community Cloud 또는 본인 PC/서버)

동작 방식
- 전체 스캔(무거움)은 신호 봉이 마감될 때만 자동 실행 (4시간봉 기준 한국시간 01·05·09·13·17·21시 직후)
- 그 사이에는 추천 코인의 현재가만 몇 초 만에 받아서 상태(진입가 근처/추격/무효/놓침)를 자동 갱신
⚠️ 참고용 화면입니다. 자동 주문 기능은 없습니다.
"""
import contextlib
import io
import threading
import traceback

import pandas as pd
import streamlit as st

import app_logic as L
import crypto_market_regime as cmr

st.set_page_config(page_title="코인 추천", page_icon="📈", layout="centered",
                   initial_sidebar_state="collapsed")
st.markdown(f"<style>{L.CSS}</style>", unsafe_allow_html=True)


@st.cache_resource
def get_store() -> dict:
    """모든 접속(폰/PC)이 분석 결과를 공유 → 접속할 때마다 새로 스캔하지 않음."""
    return {"lock": threading.Lock(), "result": None, "log": "", "error": None, "scan_cfg": None,
            "live_at": None, "live_log": "", "bt": None, "bt_error": None}


store = get_store()


def kst(ts) -> str:
    return (pd.Timestamp(ts) + pd.Timedelta(hours=9)).strftime("%H:%M")


st.title("📈 코인 추천")
st.caption("Bitget 선물용 · 스윙 신호 · 참고용(자동 주문 아님)")

# ---------------------------------------------------------------- 설정
with st.expander("⚙️ 설정"):
    c1, c2 = st.columns(2)
    balance = c1.number_input("계좌 잔고 (USDT)", min_value=10.0, value=300.0, step=50.0, key="balance")
    risk_pct = c2.number_input("트레이드당 리스크 (%)", min_value=0.1, max_value=3.0, value=1.0, step=0.1,
                               key="risk_pct", help="손절가에 닿았을 때 잃는 금액이 계좌의 몇 %인지")
    c3, c4 = st.columns(2)
    max_total = c3.number_input("총 리스크 상한 (%)", min_value=1.0, max_value=20.0, value=5.0, step=0.5,
                                key="max_total",
                                help="동시에 들고 있는 모든 포지션이 한꺼번에 손절될 때 잃는 합계 상한")
    open_pos = c4.number_input("지금 보유 중인 포지션 수", min_value=0, max_value=30, value=0, step=1,
                               key="open_pos", help="프로그램은 거래소 계좌를 볼 수 없어서 직접 입력해야 정확해요")
    max_n = st.slider("같은 방향 동시 추천 최대 개수", 1, 15, 10, key="max_n",
                      help="알트코인은 BTC와 같이 움직여서, 같은 방향을 많이 잡아도 분산이 잘 안 됩니다")
    c5, c6 = st.columns(2)
    top_n = c5.slider("거래소별 스캔 코인 수", 10, 150, 100, key="top_n", help="바꾼 뒤 '새로 분석'을 눌러야 반영")
    live_sec = c6.selectbox("가격 자동 갱신(초)", [15, 30, 60], index=1, key="live_sec")
    tf_choice = st.radio("신호 봉", ["4시간봉 (기본)", "1시간봉 (비교용)"], horizontal=True, key="tf_choice")
    bitget_only = st.checkbox("Bitget 선물 거래 가능한 코인만", value=True, key="bitget_only")

tf = "1h" if tf_choice.startswith("1") else "4h"
risk_cfg = cmr.RiskConfig(account_balance=balance, risk_per_trade_pct=risk_pct, max_concurrent_setups=max_n,
                          max_total_risk_pct=max_total, open_positions=int(open_pos))
scan_cfg = (top_n, bitget_only, tf)


# ---------------------------------------------------------------- 과거 검증 화면
def render_backtest() -> None:
    st.caption("실제 과거 데이터로 지금 전략을 그대로 돌려봐요. 과거에 좋았다고 미래가 보장되진 않아요.")
    b1, b2 = st.columns(2)
    n_coins = b1.slider("검증할 코인 수", 5, 30, 15, key="bt_n")
    period = b2.selectbox("기간", ["6개월", "1년", "2년"], index=1, key="bt_period")
    days = {"6개월": 182, "1년": 365, "2년": 730}[period]
    est_min = max(1, round(n_coins * (9 if tf == "4h" else 30) * days / 365 / 60))
    st.caption(f"{cmr.tf_label(tf)} 기준 · 예상 소요 약 {est_min}분(데이터 받는 시간 포함 더 걸릴 수 있음) · "
               "거래가 100건 이상이어야 믿을 만해요")
    if st.button("▶ 검증 실행", type="primary", use_container_width=True):
        prog = st.progress(0.0, text="과거 데이터 받는 중...")

        def cb(i: int, n: int, sym: str) -> None:
            prog.progress(min(i / max(n, 1), 1.0), text=f"검증 중 {i}/{n}  {sym}")

        with store["lock"]:
            cmr.BITGET_ONLY = bitget_only
            cmr.set_timeframe(tf)
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    store["bt"] = cmr.run_backtest_suite(n_coins, days, cb)
                store["bt_error"] = None
            except Exception:
                store["bt_error"] = traceback.format_exc() + "\n" + buf.getvalue()
            finally:  # 추천 화면이 쓰는 봉 모드로 되돌림
                r0 = store["result"]
                cmr.set_timeframe(r0.get("timeframe", "4h") if r0 else "4h")
        prog.empty()

    if store.get("bt_error"):
        st.error("검증 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
        st.code(store["bt_error"], language=None)
    bt = store.get("bt")
    if not bt:
        st.info("아직 실행한 검증이 없어요. 코인 수와 기간을 고르고 '▶ 검증 실행'을 눌러주세요.")
        return

    st.caption(f"{bt['exchange']} · {cmr.tf_label(bt['timeframe'])} · "
               f"{pd.Timestamp(bt['start']):%Y-%m-%d} ~ {pd.Timestamp(bt['end']):%Y-%m-%d} · "
               f"코인 {len(bt['coins'])}개 · 실행 {kst(bt['ran_at'])}")
    actual = bt.get("days_actual", bt["days"])
    if actual < 0.9 * bt["days"]:
        st.warning(f"요청한 {bt['days']}일 중 실제로는 {actual}일치만 받을 수 있었어요 "
                   f"(거래소가 제공하는 과거 데이터 한도). 결과는 이 기간 기준이에요.")
    st.dataframe(cmr.backtest_mode_table(bt), hide_index=True, use_container_width=True)
    lines = cmr.interpret_backtest(cmr.summarize_trades(bt["trades"]["partial_trail"]), risk_pct)
    cmp_line = cmr.compare_modes_line(bt)
    st.markdown("\n".join(f"- {x}" for x in lines + ([cmp_line] if cmp_line else [])))

    t1, t2, t3 = st.tabs(["방향·유형별", "코인별", "📋 복사용"])
    tr = bt["trades"]["partial_trail"]
    with t1:
        st.caption("분할익절+추적손절 기준")
        st.dataframe(cmr.backtest_group_table(tr, "direction"), hide_index=True, use_container_width=True)
        st.dataframe(cmr.backtest_group_table(tr, "family"), hide_index=True, use_container_width=True)
    with t2:
        st.caption("분할익절+추적손절 기준 · 합계 R 순")
        st.dataframe(cmr.backtest_group_table(tr, "symbol"), hide_index=True, use_container_width=True)
    with t3:
        st.caption("이 내용을 복사해서 보내주시면 결과를 해석하고 기준을 조정해 드릴게요.")
        st.code(cmr.backtest_report_text(bt, risk_pct), language=None)
    if bt["errors"]:
        st.caption("제외된 코인: " + " · ".join(bt["errors"][:10]))
    st.caption("ⓘ 스프레드·펀딩비·거래대금 필터는 과거 기록이 없어 검증에 반영되지 않았어요. "
               "그리고 이 결과에 맞춰 기준을 여러 번 바꾸면 과거에만 맞는 전략이 되기 쉬우니, "
               "기준을 바꿨다면 다른 기간(예: 6개월 → 2년)으로 다시 확인하세요.")


page = st.radio("화면", ["📋 추천", "🧪 과거 검증"], horizontal=True, key="page", label_visibility="collapsed")
if page.endswith("과거 검증"):
    render_backtest()
    st.stop()


def run_scan(force: bool) -> None:
    prog = st.progress(0.0, text="다른 분석이 진행 중이면 잠시 기다려요...")

    def cb(i: int, n: int, sym: str) -> None:
        prog.progress(min(i / max(n, 1), 1.0), text=f"분석 중 {i}/{n}  {sym}")

    with store["lock"]:
        r = store["result"]
        # 기다리는 동안 다른 접속이 이미 이번 봉 기준으로 스캔했다면 그 결과를 재사용
        if not force and r is not None and not cmr.needs_full_rescan(r.get("asof_utc"), r.get("timeframe")):
            prog.empty()
            return
        cmr.TOP_N_BY_VOLUME = top_n
        cmr.BITGET_ONLY = bitget_only
        cmr.set_timeframe(tf)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                res_ = cmr.run_analysis(risk_cfg, cb)
            store.update(result=res_, log=buf.getvalue(), error=None, scan_cfg=scan_cfg, live_at=cmr.utc_now())
        except Exception:
            store["error"] = traceback.format_exc()
            store["log"] = buf.getvalue()
    prog.empty()


top_l, top_r = st.columns([3, 2])
refresh = top_r.button("🔄 새로 분석", type="primary", use_container_width=True)
cur = store["result"]
if refresh or cur is None or cmr.needs_full_rescan(cur.get("asof_utc"), cur.get("timeframe")):
    run_scan(force=refresh)

res = store["result"]
if store["error"]:
    st.error("분석 중 오류가 발생했어요. 아래 내용을 그대로 복사해서 알려주세요.")
    st.code(store["error"], language=None)
if res is None:
    st.stop()
res_tf = res.get("timeframe", "4h")
top_l.caption(f"전체 분석 {kst(res['asof_utc'])} · 다음 신호 갱신 {kst(cmr.next_candle_close(res_tf))} "
              f"({cmr.tf_label(res_tf)} 마감)")
if store["scan_cfg"] != scan_cfg:
    st.info("설정(스캔 코인 수·봉 모드 등)이 바뀌었어요. '🔄 새로 분석'을 누르면 반영됩니다.")
if res_tf == "1h":
    st.caption("⚠️ 1시간봉은 비교용이에요. 신호가 잦은 대신 가짜 신호와 수수료 비중이 커집니다.")

# ---------------------------------------------------------------- 시장 국면
regime = res["regime"]
macro_hours = regime.snapshot.get("macro_span_hours", 0.0)
st.markdown(L.regime_html(regime, macro_hours), unsafe_allow_html=True)
if "btc_d" not in regime.snapshot:
    st.caption("ⓘ 이번엔 도미넌스 데이터(CoinGecko·CoinPaprika) 응답을 못 받았어요. "
               "나머지 근거로 정상 판단했고, 다음 갱신 때 다시 시도합니다.")
with st.expander("📊 판단 근거 자세히"):
    st.markdown(L.regime_detail_html(regime), unsafe_allow_html=True)

# ---------------------------------------------------------------- 서킷브레이커
breaker = cmr.circuit_breaker_triggered(risk_cfg)
if breaker:
    st.error(f"🛑 {breaker}\n\n손실 만회 목적의 추가 진입이 계좌를 가장 크게 망가뜨립니다. 오늘은 쉬세요.")
    st.stop()


# ---------------------------------------------------------------- 추천 (가격만 자동 갱신)
def live_refresh_if_due() -> None:
    r = store["result"]
    if not r or not r.get("all_setups"):
        return
    last = store.get("live_at")
    if last is not None and (cmr.utc_now() - last).total_seconds() < live_sec:
        return
    if not store["lock"].acquire(blocking=False):  # 전체 스캔 중이면 이번 갱신은 건너뜀
        return
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmr.refresh_live_status(r["all_setups"])
        store["live_at"], store["live_log"] = cmr.utc_now(), buf.getvalue()
    except Exception:
        store["live_log"] = traceback.format_exc()
    finally:
        store["lock"].release()


def render(items: list, slots: int = 10 ** 6) -> None:
    for k, s in enumerate(items):
        sizing = cmr.calculate_position_size(s.entry_price, s.sl, risk_cfg)
        st.markdown(L.card_html(s, sizing, risk_pct, over_limit=k >= slots), unsafe_allow_html=True)
        with st.expander("📋 주문 메모 (복사)"):
            st.code(L.order_memo(s, sizing, L.leverage_guide(s.entry_price, s.sl)), language=None)


def recommendations() -> None:
    # 화면을 켜둔 채 봉이 마감되면(자동 갱신 중 감지) 전체 재분석을 위해 앱 전체를 다시 실행
    r0 = store["result"]
    if fragment is not None and r0 is not None and not store["error"] \
            and cmr.needs_full_rescan(r0.get("asof_utc"), r0.get("timeframe")):
        st.rerun()
    live_refresh_if_due()
    r = store["result"]
    with contextlib.redirect_stdout(io.StringIO()):
        setups = cmr.cap_correlated_exposure(r["all_setups"], risk_cfg) if r["all_setups"] else []
    dead = [s for s in setups if s.live_status in ("invalid", "missed")]
    alive = [s for s in setups if s.live_status not in ("invalid", "missed")]
    ready = sorted([s for s in alive if s.live_status != "chase"], key=L.sort_key, reverse=True)
    waiting = sorted([s for s in alive if s.live_status == "chase"], key=L.sort_key, reverse=True)
    slots = cmr.available_slots(risk_cfg)

    if store.get("live_at") is not None:
        st.caption(f"💹 가격 갱신 {kst(store['live_at'])} · {live_sec}초마다 자동")
    if slots == 0:
        st.warning(f"총 리스크 상한 {max_total:g}%에 도달했어요 (보유 {int(open_pos)}개 × {risk_pct:g}%). "
                   "새 진입은 기존 포지션이 정리된 뒤에 검토하세요.")
    else:
        st.caption(f"🎯 새로 잡을 수 있는 포지션 {slots}개 (총 리스크 상한 {max_total:g}%, 보유 {int(open_pos)}개)")

    if not alive:
        st.info("**지금은 조건에 맞는 코인이 없어요.**\n\n"
                "추세·임펄스·상대강도·손익비·상위추세·펀딩비·스프레드 조건을 모두 통과한 코인이 없다는 뜻입니다. "
                "억지로 진입하지 않는 것도 전략이에요. 다음 봉 마감 때 다시 확인하세요.")
    else:
        tab1, tab2 = st.tabs([f"✅ 진입 검토 ({len(ready)})", f"⏳ 대기 ({len(waiting)})"])
        with tab1:
            st.caption("현재가가 진입가 근처인 코인. 우선순위 순이고, 리스크 한도 안의 코인만 진입 대상이에요.")
            if ready:
                render(ready, slots)
            else:
                st.write("지금 진입가 근처인 코인은 없어요. '대기' 탭의 지정가를 확인하세요.")
        with tab2:
            st.caption("아직 되돌림 전이라 지금 들어가면 추격인 코인. 지정가만 걸어두고, "
                       "체결될 때 리스크 한도가 남아 있는지 확인하세요.")
            if waiting:
                render(waiting)
            else:
                st.write("대기 중인 코인이 없어요.")
    if dead:
        with st.expander(f"❌ 무효·놓침 ({len(dead)}) — 다음 봉 마감 때 목록에서 정리돼요"):
            for s in dead:
                st.markdown(L.dead_line(s), unsafe_allow_html=True)


fragment = getattr(st, "fragment", None)
if fragment is not None:
    recommendations = fragment(run_every=live_sec)(recommendations)
recommendations()

# ---------------------------------------------------------------- 부가 기능
with st.expander("📖 용어 / 사용법"):
    st.markdown(
        "- **진입(지정가)**: 이 가격에 지정가 주문을 걸어요. 시장가로 쫓아 들어가지 않는 게 핵심입니다.\n"
        "- **손절**: 이 가격에 닿으면 무조건 정리. 수량은 '손절 시 손실이 계좌의 리스크 %'가 되도록 계산돼 있어요.\n"
        "- **청산 계획**: 목표1에서 절반 익절 → 남은 절반의 손절을 진입가로 올림 → 이후 최고가(롱)·최저가(숏)에서 "
        "카드에 적힌 폭만큼 되돌리면 정리(추적 손절). 목표2는 참고선이에요.\n"
        "- **손익비**: (목표1까지 거리) ÷ (손절까지 거리).\n"
        "- **상태**: ✅ 진입가 근처 / ⏳ 추격 구간(대기) / ❌ 무효(손절선을 먼저 넘음) / ⌛ 놓침(목표1에 먼저 닿음). "
        "가격만 자동 갱신하고, 신호 자체는 봉 마감 때만 새로 계산해요.\n"
        "- **총 리스크 상한**: 보유 포지션과 새 진입을 합쳐 한꺼번에 손절돼도 이 % 이상 잃지 않도록 진입 수를 제한해요.\n"
        "- **↔ 시장 역행**: 시장 전체 방향과 반대로 가는 코인. 개별적으로는 근거가 있지만 거시 흐름을 거스르니 비중을 작게.\n"
        "- **레버리지 상한 가이드**: 청산가가 손절가보다 훨씬 멀리 있도록 잡은 상한(최대 10배, 격리마진 기준).\n"
        "- **🎯 매물대 겹침 / 🩸 유동성 스윕**: 신뢰도를 높여주는 보조 근거예요."
    )

with st.expander("📝 매매 결과 기록 (서킷브레이커용)"):
    st.caption("하루 손실 5% / 주간 10%를 넘으면 신규 추천을 자동 중단합니다. "
               "※ Streamlit Cloud에서는 앱이 재시작되면 기록이 초기화될 수 있어요.")
    lc1, lc2 = st.columns(2)
    pnl = lc1.number_input("손익 (USDT, 손실은 음수)", value=0.0, step=1.0, key="pnl_in")
    sym_in = lc2.text_input("코인 (선택)", key="pnl_sym")
    if st.button("기록 저장"):
        cmr.log_trade_result(float(pnl), sym_in)
        st.success("저장했어요.")
        st.rerun()

fs = res.get("filter_stats") or {}
if fs.get("universe"):
    with st.expander(f"🧮 필터 통과 현황 ({fs.get('universe', 0)}개 → {fs.get('passed', 0)}개)"):
        st.caption("각 조건에서 몇 개가 걸러졌는지예요. 한 조건이 거의 다 걸러내거나 아무것도 못 거르면 기준 점검이 필요해요.")
        rows = sorted((fs.get("rejects") or {}).items(), key=lambda kv: -kv[1])
        if rows:
            st.table(pd.DataFrame([{"제외 사유": cmr.REJECT_LABELS.get(k, k), "코인 수": v} for k, v in rows]))

if store["log"].strip() or store.get("live_log", "").strip():
    with st.expander("🔍 분석 로그"):
        st.code((store["log"] + "\n" + store.get("live_log", "")).strip(), language=None)

st.caption("⚠️ 참고용 정보이며 수익을 보장하지 않습니다. 손절은 반드시 지키고, 감당 가능한 금액만 사용하세요.")
