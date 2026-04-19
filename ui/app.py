"""AML Investigation Agent — Streamlit Dashboard."""
from __future__ import annotations

import structlog
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from ui.queries import (  # noqa: E402
    get_compliance_reports,
    get_cost_metrics,
    get_escalation_queue,
    get_open_investigations,
    get_operational_metrics,
)

log = structlog.get_logger()

st.set_page_config(
    page_title="AML Agent",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Design system ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;500;600&display=swap');

/* ── Root tokens ── */
:root {
    --bg:        #080A0F;
    --surface:   #0E1118;
    --surface2:  #141824;
    --border:    #1E2535;
    --border2:   #2A3347;
    --text:      #CDD6F0;
    --muted:     #4A5568;
    --accent:    #3B82F6;

    --red:       #EF4444;
    --red-dim:   rgba(239,68,68,0.12);
    --amber:     #F59E0B;
    --amber-dim: rgba(245,158,11,0.12);
    --green:     #22C55E;
    --green-dim: rgba(34,197,94,0.12);
    --blue-dim:  rgba(59,130,246,0.12);

    --mono: 'JetBrains Mono', 'Fira Code', monospace;
    --sans: 'Inter', -apple-system, sans-serif;
    --radius: 6px;
}

/* ── Global resets ── */
html, body, [class*="css"] {
    font-family: var(--sans) !important;
    background-color: var(--bg) !important;
    color: var(--text) !important;
}

/* ── Hide Streamlit chrome ── */
#MainMenu, footer, header { visibility: hidden; }
.stDeployButton { display: none; }
.block-container { padding-top: 1.5rem !important; padding-bottom: 2rem !important; }

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background-color: var(--surface) !important;
    border-right: 1px solid var(--border) !important;
}
[data-testid="stSidebar"] .stRadio label {
    font-family: var(--sans) !important;
    font-size: 0.85rem !important;
    color: var(--muted) !important;
    padding: 0.35rem 0 !important;
    transition: color 0.15s;
}
[data-testid="stSidebar"] .stRadio label:hover { color: var(--text) !important; }
[data-testid="stSidebar"] .stRadio [aria-checked="true"] + div label {
    color: var(--text) !important;
}

/* ── Metric cards ── */
[data-testid="stMetric"] {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    padding: 1rem 1.25rem !important;
}
[data-testid="stMetricLabel"] { color: var(--muted) !important; font-size: 0.75rem !important; letter-spacing: 0.08em; text-transform: uppercase; }
[data-testid="stMetricValue"] { font-family: var(--mono) !important; font-size: 1.6rem !important; color: var(--text) !important; }

/* ── Dataframe ── */
[data-testid="stDataFrame"] { border: 1px solid var(--border) !important; border-radius: var(--radius) !important; }

/* ── Expander ── */
[data-testid="stExpander"] {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    margin-bottom: 0.5rem !important;
}
[data-testid="stExpander"] summary { color: var(--text) !important; }

/* ── Divider ── */
hr { border-color: var(--border) !important; margin: 1rem 0 !important; }

/* ── Selectbox ── */
[data-testid="stSelectbox"] select,
.stSelectbox > div > div {
    background: var(--surface2) !important;
    border-color: var(--border) !important;
    color: var(--text) !important;
    font-family: var(--mono) !important;
    font-size: 0.85rem !important;
}

/* ── Button ── */
.stButton > button {
    background: var(--surface2) !important;
    border: 1px solid var(--border2) !important;
    color: var(--text) !important;
    font-family: var(--sans) !important;
    font-size: 0.8rem !important;
    letter-spacing: 0.05em;
    border-radius: var(--radius) !important;
    transition: border-color 0.15s, background 0.15s;
}
.stButton > button:hover {
    border-color: var(--accent) !important;
    background: var(--blue-dim) !important;
}

/* ── Info / warning boxes ── */
[data-testid="stAlertContainer"] {
    background: var(--surface2) !important;
    border-color: var(--border2) !important;
    border-radius: var(--radius) !important;
}

/* ── Custom components ── */
.page-header {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border);
}
.page-header h2 {
    margin: 0;
    font-family: var(--sans);
    font-size: 1.1rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text);
}
.page-header .count-pill {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--muted);
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 2px 10px;
}

/* ── Investigation card ── */
.inv-card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.85rem 1rem;
    margin-bottom: 0.5rem;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0.5rem;
    align-items: center;
    transition: border-color 0.15s;
}
.inv-card:hover { border-color: var(--border2); }
.inv-card .left { display: flex; flex-direction: column; gap: 0.2rem; }
.inv-card .txn-id {
    font-family: var(--mono);
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text);
}
.inv-card .inv-meta {
    font-family: var(--mono);
    font-size: 0.72rem;
    color: var(--muted);
}
.inv-card .right { display: flex; gap: 0.5rem; align-items: center; }

/* ── Status & verdict badges ── */
.badge {
    font-family: var(--mono);
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 3px 9px;
    border-radius: 4px;
    border: 1px solid transparent;
    white-space: nowrap;
}
.badge-resolved   { color: var(--green); background: var(--green-dim); border-color: rgba(34,197,94,0.3); }
.badge-escalated  { color: var(--amber); background: var(--amber-dim); border-color: rgba(245,158,11,0.3); }
.badge-investigating { color: var(--accent); background: var(--blue-dim); border-color: rgba(59,130,246,0.3); }
.badge-unknown    { color: var(--muted); background: var(--surface); border-color: var(--border); }

.badge-suspicious { color: var(--red);   background: var(--red-dim);   border-color: rgba(239,68,68,0.3); }
.badge-clean      { color: var(--green); background: var(--green-dim); border-color: rgba(34,197,94,0.3); }
.badge-inconclusive { color: var(--amber); background: var(--amber-dim); border-color: rgba(245,158,11,0.3); }

.badge-max_hops   { color: var(--red);   background: var(--red-dim);   border-color: rgba(239,68,68,0.3); }
.badge-low_confidence { color: var(--amber); background: var(--amber-dim); border-color: rgba(245,158,11,0.3); }
.badge-timeout    { color: var(--amber); background: var(--amber-dim); border-color: rgba(245,158,11,0.3); }
.badge-cost_cap   { color: var(--red);   background: var(--red-dim);   border-color: rgba(239,68,68,0.3); }
.badge-empty_evidence { color: var(--muted); background: var(--surface); border-color: var(--border); }

/* ── Trigger badge ── */
.trigger-badge {
    font-family: var(--mono);
    font-size: 0.62rem;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 7px;
}

/* ── Report card ── */
.report-card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1rem 1.25rem;
    margin-bottom: 0.75rem;
}
.report-card .report-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 0.75rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border);
}
.report-card .report-title {
    font-family: var(--mono);
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text);
}
.report-card .report-sub {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--muted);
    margin-top: 0.2rem;
}
.report-card .report-finding {
    font-size: 0.82rem;
    color: var(--text);
    line-height: 1.5;
    margin-bottom: 0.75rem;
}
.report-card .report-narrative {
    font-size: 0.78rem;
    color: var(--muted);
    font-style: italic;
    line-height: 1.6;
    border-left: 2px solid var(--border2);
    padding-left: 0.75rem;
    margin-bottom: 0.75rem;
}

/* ── Evidence timeline ── */
.evidence-chain { margin-top: 0.5rem; }
.evidence-item {
    display: flex;
    gap: 0.75rem;
    align-items: flex-start;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--border);
    font-size: 0.78rem;
}
.evidence-item:last-child { border-bottom: none; }
.evidence-hop {
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 50%;
    width: 22px;
    height: 22px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    margin-top: 1px;
}
.evidence-tool {
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--accent);
    white-space: nowrap;
    margin-top: 2px;
}
.evidence-finding { color: var(--text); line-height: 1.5; }
.significance-high   { color: var(--red); }
.significance-medium { color: var(--amber); }
.significance-low    { color: var(--muted); }

/* ── Stat row ── */
.stat-row {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-top: 0.4rem;
}
.stat-chip {
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 8px;
}

/* ── Cost bar ── */
.stBarChart { background: var(--surface2) !important; }

/* ── Section label ── */
.section-label {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 0.5rem;
}

/* ── Escalation card ── */
.esc-card {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-left: 3px solid var(--amber);
    border-radius: var(--radius);
    padding: 0.85rem 1rem;
    margin-bottom: 0.5rem;
}
.esc-card.reason-max_hops   { border-left-color: var(--red); }
.esc-card.reason-cost_cap   { border-left-color: var(--red); }
.esc-card.reason-low_confidence { border-left-color: var(--amber); }
.esc-card.reason-timeout    { border-left-color: var(--amber); }
.esc-card .esc-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.esc-card .esc-txn { font-family: var(--mono); font-size: 0.88rem; font-weight: 600; }
.esc-card .esc-evidence { font-size: 0.78rem; color: var(--muted); margin-top: 0.4rem; }
.esc-card .esc-id { font-family: var(--mono); font-size: 0.65rem; color: var(--muted); margin-top: 0.3rem; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _badge(cls: str, label: str) -> str:
    return f'<span class="badge badge-{cls}">{label}</span>'


def _status_badge(status: str) -> str:
    s = (status or "unknown").lower()
    label_map = {
        "resolved": "RESOLVED",
        "escalated": "ESCALATED",
        "investigating": "ACTIVE",
        "tool_calling": "ACTIVE",
        "evaluating": "ACTIVE",
    }
    return _badge(s if s in ("resolved", "escalated", "investigating") else "unknown",
                  label_map.get(s, s.upper()))


def _verdict_badge(verdict: str) -> str:
    v = (verdict or "").lower()
    return _badge(v if v in ("suspicious", "clean", "inconclusive") else "unknown",
                  v.upper() if v else "—")


def _reason_badge(reason: str) -> str:
    r = (reason or "").lower()
    label_map = {
        "max_hops": "MAX HOPS",
        "low_confidence": "LOW CONF",
        "timeout": "TIMEOUT",
        "cost_cap": "COST CAP",
        "empty_evidence": "NO EVIDENCE",
    }
    return _badge(r if r in label_map else "unknown", label_map.get(r, r.upper()))


def _trigger_badge(trigger: str) -> str:
    t = (trigger or "unknown").lower()
    label_map = {
        "rule_engine": "RULE",
        "ml_score": "ML",
        "both": "RULE+ML",
        "unknown": "UNKNOWN",
    }
    return f'<span class="trigger-badge">{label_map.get(t, t.upper())}</span>'


def _significance_class(sig: str) -> str:
    return f"significance-{(sig or 'low').lower()}"


# ── Sidebar ───────────────────────────────────────────────────────────────────

VIEWS = [
    "Investigations",
    "Compliance Reports",
    "Escalation Queue",
    "Cost Metrics",
    "Operational Metrics",
]

with st.sidebar:
    st.markdown("""
    <div style="padding: 0.5rem 0 1rem 0; border-bottom: 1px solid var(--border); margin-bottom: 1rem;">
        <div style="font-family: var(--mono); font-size: 0.7rem; color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 0.2rem;">AML AGENT</div>
        <div style="font-family: var(--sans); font-size: 1rem; font-weight: 600; color: var(--text);">Investigation Console</div>
    </div>
    """, unsafe_allow_html=True)

    selected_view = st.radio("Navigation", VIEWS, index=0, label_visibility="collapsed")

    st.markdown("<div style='flex: 1'></div>", unsafe_allow_html=True)

    st.markdown("""
    <div style="font-family: var(--mono); font-size: 0.62rem; color: var(--muted); line-height: 1.8; padding: 0.5rem 0;">
        <div>POSTGRES · REDIS · KAFKA</div>
        <div style="margin-top: 0.2rem; color: var(--border2);">v0.6.0 · Phase 6</div>
    </div>
    """, unsafe_allow_html=True)

    if st.button("↻ Refresh", use_container_width=True):
        st.rerun()


# ── View: Investigations ───────────────────────────────────────────────────────

if selected_view == "Investigations":
    rows = get_open_investigations(limit=100)

    st.markdown(f"""
    <div class="page-header">
        <h2>Investigations</h2>
        <span class="count-pill">{len(rows)} total</span>
    </div>
    """, unsafe_allow_html=True)

    if not rows:
        st.info("No investigations found. Run `python scripts/seed.py` to load demo data.")
    else:
        for r in rows:
            started = str(r.get("started_at", ""))[:19].replace("T", " ") if r.get("started_at") else "—"
            inv_id_short = str(r.get("investigation_id", ""))[:8]
            st.markdown(f"""
            <div class="inv-card">
                <div class="left">
                    <div class="txn-id">{r.get("txn_id", "—")}</div>
                    <div class="inv-meta">
                        {inv_id_short}...
                        &nbsp;·&nbsp; {started}
                    </div>
                </div>
                <div class="right">
                    {_trigger_badge(r.get("trigger_type", ""))}
                    {_status_badge(r.get("status", ""))}
                </div>
            </div>
            """, unsafe_allow_html=True)


# ── View: Compliance Reports ──────────────────────────────────────────────────

elif selected_view == "Compliance Reports":
    reports = get_compliance_reports(limit=50)

    st.markdown(f"""
    <div class="page-header">
        <h2>Compliance Reports</h2>
        <span class="count-pill">{len(reports)} resolved</span>
    </div>
    """, unsafe_allow_html=True)

    if not reports:
        st.info("No resolved investigations yet.")
    else:
        for r in reports:
            verdict = r.get("verdict", "")
            confidence = r.get("confidence", 0)
            hops = r.get("total_hops", 0)
            cost = r.get("total_cost_usd", 0)
            resolved = str(r.get("resolved_at", ""))[:19].replace("T", " ")
            inv_id_short = str(r.get("investigation_id", ""))[:8]
            evidence = r.get("evidence_chain", [])

            evidence_html = ""
            for e in evidence:
                sig_cls = _significance_class(e.get("significance", ""))
                evidence_html += f"""
                <div class="evidence-item">
                    <div class="evidence-hop">{e.get('hop', 0)}</div>
                    <div>
                        <div class="evidence-tool">{e.get('tool', '—')}</div>
                        <div class="evidence-finding {sig_cls}">{e.get('finding', '—')}</div>
                    </div>
                </div>"""

            rec = (r.get("recommendation") or "—").replace("_", " ").upper()

            st.markdown(f"""
            <div class="report-card">
                <div class="report-header">
                    <div>
                        <div class="report-title">{r.get("txn_id", "—")}</div>
                        <div class="report-sub">{inv_id_short}... · {resolved}</div>
                    </div>
                    <div style="display:flex; gap:0.4rem; align-items:center;">
                        <span class="stat-chip">{hops} hop{'s' if hops != 1 else ''}</span>
                        <span class="stat-chip">${cost:.5f}</span>
                        <span class="stat-chip">{confidence:.0%} conf</span>
                        {_verdict_badge(verdict)}
                    </div>
                </div>
                <div class="report-finding">{r.get("finding", "—")}</div>
                <div class="report-narrative">{r.get("narrative", "—")}</div>
                <div class="section-label">Evidence Chain</div>
                <div class="evidence-chain">{evidence_html or '<div class="evidence-finding" style="color:var(--muted)">No evidence recorded</div>'}</div>
                <div style="margin-top:0.75rem; padding-top:0.5rem; border-top:1px solid var(--border); display:flex; gap:0.5rem; align-items:center;">
                    <div class="section-label" style="margin:0;">Recommendation</div>
                    <span class="stat-chip" style="color:var(--text)">{rec}</span>
                </div>
            </div>
            """, unsafe_allow_html=True)


# ── View: Escalation Queue ────────────────────────────────────────────────────

elif selected_view == "Escalation Queue":
    status_filter = st.selectbox(
        "Status",
        ["open", "assigned", "resolved"],
        index=0,
        label_visibility="collapsed",
    )
    rows = get_escalation_queue(status_filter=status_filter, limit=100)

    st.markdown(f"""
    <div class="page-header">
        <h2>Escalation Queue</h2>
        <span class="count-pill">{len(rows)} {status_filter}</span>
    </div>
    """, unsafe_allow_html=True)

    if not rows:
        st.markdown(f"""
        <div style="padding:2rem; text-align:center; color:var(--muted); font-family:var(--mono); font-size:0.8rem;">
            No {status_filter!r} escalations
        </div>
        """, unsafe_allow_html=True)
    else:
        for r in rows:
            reason = (r.get("escalation_reason") or "unknown").lower()
            inv_id_short = str(r.get("investigation_id", ""))[:8]
            confidence = r.get("confidence")
            conf_str = f"{confidence:.2f}" if confidence is not None else "—"
            created = str(r.get("created_at", ""))[:19].replace("T", " ")

            st.markdown(f"""
            <div class="esc-card reason-{reason}">
                <div class="esc-header">
                    <div>
                        <span class="txn-id" style="font-family:var(--mono);font-size:0.88rem;font-weight:600;">{r.get("txn_id", "—")}</span>
                    </div>
                    <div style="display:flex;gap:0.4rem;">
                        <span class="stat-chip">conf {conf_str}</span>
                        {_reason_badge(reason)}
                    </div>
                </div>
                <div class="esc-evidence">{r.get("evidence_summary", "no evidence gathered")}</div>
                <div class="esc-id">{inv_id_short}... · {created}</div>
            </div>
            """, unsafe_allow_html=True)


# ── View: Cost Metrics ────────────────────────────────────────────────────────

elif selected_view == "Cost Metrics":
    data = get_cost_metrics()

    st.markdown("""
    <div class="page-header">
        <h2>Cost Metrics</h2>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Resolved Investigations", data["investigation_count"])
    c2.metric("Total Spend (USD)", f"${data['total_cost_usd']:.5f}")
    c3.metric("Avg Cost / Investigation", f"${data['avg_cost_usd']:.5f}")

    if data["costs"]:
        st.markdown("<div style='height:1.5rem'></div>", unsafe_allow_html=True)
        st.markdown('<div class="section-label">Cost per Investigation — Most Recent First</div>',
                    unsafe_allow_html=True)
        import pandas as pd
        df = pd.DataFrame({"cost_usd": data["costs"]})
        st.bar_chart(df["cost_usd"], color="#3B82F6", use_container_width=True)

        cap_pct = sum(1 for c in data["costs"] if c >= 0.05) / len(data["costs"]) * 100
        st.markdown(f"""
        <div class="stat-row" style="margin-top:0.5rem;">
            <span class="stat-chip">min ${min(data["costs"]):.5f}</span>
            <span class="stat-chip">max ${max(data["costs"]):.5f}</span>
            <span class="stat-chip">{cap_pct:.0f}% at cap</span>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.info("No resolved investigations yet.")


# ── View: Operational Metrics ─────────────────────────────────────────────────

elif selected_view == "Operational Metrics":
    data = get_operational_metrics()

    st.markdown("""
    <div class="page-header">
        <h2>Operational Metrics</h2>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Investigations", data["total_investigations"])
    c2.metric("Total Escalations", data["total_escalations"])
    c3.metric("Escalation Rate", f"{data['escalation_rate_pct']:.1f}%")

    st.markdown("<div style='height:0.75rem'></div>", unsafe_allow_html=True)

    c4, c5 = st.columns(2)
    c4.metric("Avg Hops / Investigation", f"{data['avg_hops']:.2f}")
    c5.metric("Avg Tool Latency", f"{data['avg_latency_ms']:.0f} ms")

    if data["total_investigations"] == 0:
        st.info("No investigation data yet. Run `python scripts/seed.py` to populate.")
    else:
        st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
        resolved = data["total_investigations"] - data["total_escalations"]
        st.markdown(f"""
        <div class="section-label">Resolution Breakdown</div>
        <div style="display:flex; gap:0.75rem; margin-top:0.4rem;">
            <div class="esc-card" style="flex:1; border-left-color:var(--green); padding:0.75rem 1rem;">
                <div style="font-family:var(--mono); font-size:1.4rem; font-weight:600; color:var(--green)">{resolved}</div>
                <div style="font-family:var(--mono); font-size:0.65rem; color:var(--muted); margin-top:0.2rem; letter-spacing:0.08em;">RESOLVED</div>
            </div>
            <div class="esc-card" style="flex:1; border-left-color:var(--amber); padding:0.75rem 1rem;">
                <div style="font-family:var(--mono); font-size:1.4rem; font-weight:600; color:var(--amber)">{data["total_escalations"]}</div>
                <div style="font-family:var(--mono); font-size:0.65rem; color:var(--muted); margin-top:0.2rem; letter-spacing:0.08em;">ESCALATED</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
