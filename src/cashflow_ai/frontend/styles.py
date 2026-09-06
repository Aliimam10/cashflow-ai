"""Application-wide visual theme for the local Streamlit interface."""

from __future__ import annotations

import streamlit as st

APP_STYLES = """
<style>
    :root {
        --cf-base: #080B10;
        --cf-surface: #0F1419;
        --cf-card: #171D26;
        --cf-border: #1E2736;
        --cf-accent: #5B8DEF;
        --cf-accent-soft: rgba(91, 141, 239, 0.14);
        --cf-amber: #F5A623;
        --cf-green: #52D98F;
        --cf-red: #FF6B78;
        --cf-text: #F3F6FB;
        --cf-muted: #94A0B2;
        --cf-subtle: #697587;
        --cf-label-font: "DM Sans", Inter, ui-sans-serif, -apple-system,
            BlinkMacSystemFont, "Segoe UI", sans-serif;
        --cf-number-font: "JetBrains Mono", "SFMono-Regular", Consolas,
            "Liberation Mono", monospace;
    }

    html, body, [class*="css"] {
        font-family: var(--cf-label-font);
    }

    .stApp {
        color: var(--cf-text);
        background:
            radial-gradient(
                circle at 78% -8%,
                rgba(91, 141, 239, 0.12),
                transparent 34rem
            ),
            var(--cf-base);
    }

    header[data-testid="stHeader"] {
        height: 2rem;
        background: transparent;
    }

    [data-testid="stToolbar"],
    [data-testid="stDecoration"],
    #MainMenu,
    footer {
        display: none !important;
    }

    .block-container {
        max-width: 1440px;
        padding: 2.2rem 3rem 5rem;
    }

    section[data-testid="stSidebar"] {
        background: #0B0F15;
        border-right: 1px solid var(--cf-border);
    }

    section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        padding: 1.5rem 1rem;
    }

    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] label {
        color: #B6C0CF;
    }

    .cf-brand {
        display: flex;
        align-items: center;
        padding: 0.35rem 0.65rem 1.8rem;
    }

    .cf-brand-mark {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2.3rem;
        height: 2.3rem;
        margin-right: 0.7rem;
        border: 1px solid rgba(91, 141, 239, 0.45);
        border-radius: 0.7rem;
        color: #DDE8FF;
        background: var(--cf-accent-soft);
        font-family: var(--cf-number-font);
        font-weight: 700;
    }

    .cf-brand-name {
        color: var(--cf-text);
        font-size: 1.08rem;
        font-weight: 750;
        letter-spacing: -0.02em;
    }

    .cf-menu-label {
        margin: 0 0.65rem 0.55rem;
        color: #596678;
        font-size: 0.66rem;
        font-weight: 750;
        letter-spacing: 0.14em;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] {
        gap: 0.35rem;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        padding: 0.72rem 0.78rem;
        border: 1px solid transparent;
        border-radius: 0.68rem;
        transition: background 120ms ease, border-color 120ms ease;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
        background: rgba(255, 255, 255, 0.035);
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
        border-color: rgba(91, 141, 239, 0.22);
        background: var(--cf-accent-soft);
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) p {
        color: #DDE8FF;
        font-weight: 700;
    }

    .cf-sidebar-footer {
        margin: 1.5rem 0.65rem 0;
        padding: 0.9rem;
        border: 1px solid var(--cf-border);
        border-radius: 0.75rem;
        color: #7F8A9B;
        background: var(--cf-surface);
        font-size: 0.76rem;
        line-height: 1.5;
    }

    .cf-sidebar-footer strong {
        color: #AEB9C8;
    }

    .cf-page-header {
        max-width: 780px;
        margin: 0 0 1.35rem;
    }

    .cf-eyebrow,
    .cf-balance-kicker {
        color: var(--cf-accent);
        font-size: 0.68rem;
        font-weight: 800;
        letter-spacing: 0.14em;
        text-transform: uppercase;
    }

    .cf-eyebrow {
        margin-bottom: 0.55rem;
    }

    .cf-page-title {
        margin: 0;
        color: var(--cf-text);
        font-size: clamp(2rem, 4vw, 3.15rem);
        line-height: 1.05;
        letter-spacing: -0.048em;
    }

    .cf-page-description {
        max-width: 720px;
        margin: 0.75rem 0 0;
        color: var(--cf-muted);
        font-size: 1rem;
        line-height: 1.6;
    }

    .cf-status {
        display: inline-flex;
        align-items: center;
        gap: 0.5rem;
        margin: 0 0 1.25rem;
        padding: 0.42rem 0.7rem;
        border: 1px solid rgba(82, 217, 143, 0.24);
        border-radius: 999px;
        color: var(--cf-green);
        background: rgba(82, 217, 143, 0.08);
        font-size: 0.76rem;
        font-weight: 700;
    }

    .cf-status-dot {
        width: 0.45rem;
        height: 0.45rem;
        border-radius: 50%;
        background: var(--cf-green);
        box-shadow: 0 0 0 4px rgba(82, 217, 143, 0.1);
    }

    .cf-status.is-warning {
        border-color: rgba(245, 166, 35, 0.3);
        color: var(--cf-amber);
        background: rgba(245, 166, 35, 0.08);
    }

    .cf-status.is-warning .cf-status-dot {
        background: var(--cf-amber);
        box-shadow: 0 0 0 4px rgba(245, 166, 35, 0.1);
    }

    .cf-balance-hero {
        display: grid;
        grid-template-columns: minmax(240px, 0.85fr) minmax(360px, 1.6fr);
        gap: 2rem;
        align-items: stretch;
        min-height: 245px;
        margin: 0.65rem 0 1.4rem;
        padding: 1.65rem 1.75rem;
        overflow: hidden;
        border: 1px solid var(--cf-border);
        border-radius: 1.05rem;
        background:
            linear-gradient(120deg, rgba(91, 141, 239, 0.09), transparent 45%),
            var(--cf-card);
        box-shadow: 0 20px 50px rgba(0, 0, 0, 0.2);
    }

    .cf-balance-copy {
        display: flex;
        flex-direction: column;
        justify-content: center;
    }

    .cf-balance-value {
        margin: 0.55rem 0 0.3rem;
        color: #FFFFFF;
        font-family: var(--cf-number-font);
        font-size: clamp(2rem, 4.4vw, 3.65rem);
        font-weight: 650;
        letter-spacing: -0.055em;
    }

    .cf-balance-meta {
        color: var(--cf-muted);
        font-size: 0.82rem;
    }

    .cf-balance-change {
        margin-top: 0.85rem;
        font-family: var(--cf-number-font);
        font-size: 0.8rem;
    }

    .cf-balance-change.is-positive { color: var(--cf-green); }
    .cf-balance-change.is-negative { color: var(--cf-red); }
    .cf-balance-change.is-neutral,
    .cf-balance-change.is-unavailable { color: var(--cf-muted); }

    .cf-balance-pulse {
        display: flex;
        min-width: 0;
        align-items: center;
    }

    .cf-pulse {
        width: 100%;
        min-height: 160px;
    }

    .cf-pulse-svg {
        width: 100%;
        height: 160px;
        overflow: visible;
        filter: drop-shadow(0 0 10px rgba(91, 141, 239, 0.22));
    }

    .cf-pulse-line {
        fill: none;
        stroke: var(--cf-accent);
        stroke-width: 3;
        stroke-linecap: round;
        stroke-linejoin: round;
        vector-effect: non-scaling-stroke;
        stroke-dasharray: 1200;
        stroke-dashoffset: 1200;
        animation: cf-draw-pulse 1.2s cubic-bezier(.22,.75,.25,1) forwards;
    }

    .cf-pulse-line--account-1 { stroke: var(--cf-green); }
    .cf-pulse-line--account-2 { stroke: var(--cf-amber); }

    .cf-pulse-point {
        fill: var(--cf-accent);
        stroke: rgba(255, 255, 255, 0.35);
        stroke-width: 1;
    }

    .cf-pulse--empty {
        display: grid;
        place-items: center;
        min-height: 160px;
        border: 1px dashed var(--cf-border);
        border-radius: 0.85rem;
        color: var(--cf-subtle);
        background: rgba(8, 11, 16, 0.35);
    }

    @keyframes cf-draw-pulse {
        to { stroke-dashoffset: 0; }
    }

    .cf-feature-card,
    .cf-activity-card,
    div[data-testid="stMetric"],
    div[data-testid="stForm"],
    details[data-testid="stExpander"] {
        border: 1px solid var(--cf-border);
        border-radius: 0.9rem;
        background: var(--cf-card);
        box-shadow: none;
    }

    .cf-feature-card {
        min-height: 168px;
        padding: 1.2rem;
    }

    .cf-activity-card {
        margin: 1rem 0 1.35rem;
        padding: 0.25rem 1.1rem;
    }

    .cf-section-heading,
    .cf-transaction-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
    }

    .cf-section-heading {
        padding: 1rem 0 0.75rem;
    }

    .cf-section-heading span {
        color: var(--cf-accent);
        font-size: 0.64rem;
        font-weight: 800;
        letter-spacing: 0.13em;
    }

    .cf-section-heading h3 {
        margin: 0.16rem 0 0;
        font-size: 1.02rem;
    }

    .cf-count-pill {
        padding: 0.25rem 0.55rem;
        border: 1px solid var(--cf-border);
        border-radius: 999px;
        color: var(--cf-muted);
        background: var(--cf-surface);
        font-family: var(--cf-number-font);
        font-size: 0.68rem;
    }

    .cf-transaction-row {
        min-height: 58px;
        padding: 0.65rem 0;
        border-top: 1px solid var(--cf-border);
    }

    .cf-transaction-main {
        display: flex;
        min-width: 0;
        flex-direction: column;
    }

    .cf-transaction-main strong {
        overflow: hidden;
        color: #E5EBF5;
        font-size: 0.86rem;
        font-weight: 650;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .cf-transaction-meta {
        margin-top: 0.18rem;
        color: var(--cf-subtle);
        font-size: 0.72rem;
    }

    .cf-transaction-amount {
        flex: none;
        font-family: var(--cf-number-font);
        font-size: 0.82rem;
        font-weight: 650;
    }

    .cf-transaction-amount.is-positive { color: var(--cf-green); }
    .cf-transaction-amount.is-negative { color: #D7DFEA; }
    .cf-transaction-amount.is-neutral { color: var(--cf-muted); }

    .cf-feature-icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2.2rem;
        height: 2.2rem;
        margin-bottom: 0.8rem;
        border-radius: 0.65rem;
        color: #DDE8FF;
        background: var(--cf-accent-soft);
    }

    .cf-feature-card h3 {
        margin: 0 0 0.4rem;
        color: var(--cf-text);
        font-size: 0.98rem;
    }

    .cf-feature-card p {
        margin: 0;
        color: var(--cf-muted);
        font-size: 0.86rem;
        line-height: 1.5;
    }

    .cf-notice,
    .cf-empty-state {
        margin: 0.75rem 0 1rem;
        padding: 0.85rem 1rem;
        border: 1px solid var(--cf-border);
        border-left-width: 3px;
        border-radius: 0.72rem;
        color: #B5C0CF;
        background: var(--cf-surface);
        font-size: 0.84rem;
        line-height: 1.5;
    }

    .cf-notice strong,
    .cf-empty-state strong { color: var(--cf-text); }
    .cf-notice.is-private { border-left-color: var(--cf-accent); }
    .cf-notice.is-caution { border-left-color: var(--cf-amber); }
    .cf-empty-state { border-left-color: var(--cf-accent); }

    div[data-testid="stMetric"] {
        min-height: 112px;
        padding: 1rem 1.05rem;
    }

    div[data-testid="stMetric"] [data-testid="stMetricLabel"] p {
        color: var(--cf-muted);
        font-size: 0.78rem;
        font-weight: 650;
    }

    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: var(--cf-text);
        font-family: var(--cf-number-font);
        font-size: 1.55rem;
        letter-spacing: -0.035em;
    }

    div[data-testid="stFileUploader"] section {
        border: 1.5px dashed #35445B;
        border-radius: 0.9rem;
        background: var(--cf-surface);
    }

    div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div,
    div[data-baseweb="base-input"],
    textarea,
    [data-testid="stDateInput"] input {
        border-color: var(--cf-border) !important;
        color: var(--cf-text) !important;
        background: var(--cf-surface) !important;
    }

    .stButton > button,
    .stFormSubmitButton > button,
    .stDownloadButton > button {
        min-height: 2.55rem;
        padding: 0.5rem 1rem;
        border-radius: 0.65rem;
        border-color: #2A3850;
        color: #E7EDFA;
        background: #151C27;
        font-weight: 700;
    }

    .stButton > button[kind="primary"],
    .stFormSubmitButton > button[kind="primary"] {
        border-color: var(--cf-accent);
        background: var(--cf-accent);
        color: #07101E;
    }

    .stButton > button:hover,
    .stFormSubmitButton > button:hover,
    .stDownloadButton > button:hover {
        border-color: var(--cf-accent);
        color: #FFFFFF;
    }

    div[data-testid="stSegmentedControl"] [data-baseweb="button-group"] {
        padding: 0.2rem;
        border: 1px solid var(--cf-border);
        border-radius: 0.72rem;
        background: var(--cf-surface);
    }

    div[data-testid="stSegmentedControl"] button[aria-pressed="true"] {
        color: #DDE8FF;
        background: var(--cf-accent-soft);
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.35rem;
        border-bottom-color: var(--cf-border);
        overflow-x: auto;
    }

    .stTabs [data-baseweb="tab"] {
        min-width: max-content;
        color: var(--cf-muted);
    }

    [data-testid="stDataFrame"],
    [data-testid="stVegaLiteChart"] {
        overflow: hidden;
        border: 1px solid var(--cf-border);
        border-radius: 0.85rem;
        background: var(--cf-card);
    }

    div[data-testid="stAlert"] {
        border-color: var(--cf-border);
        border-radius: 0.75rem;
        background: var(--cf-surface);
    }

    h1, h2, h3 {
        color: var(--cf-text);
        letter-spacing: -0.03em;
    }

    h2 { margin-top: 1.35rem; }
    p, label { color: #C7D0DC; }
    small, .stCaption { color: var(--cf-muted); }
    hr { border-color: var(--cf-border); }

    code, pre, kbd {
        font-family: var(--cf-number-font);
    }

    @media (prefers-reduced-motion: reduce) {
        .cf-pulse-line {
            animation: none;
            stroke-dashoffset: 0;
        }
    }

    @media (max-width: 900px) {
        .block-container { padding: 1.8rem 1.4rem 4rem; }
        .cf-balance-hero {
            grid-template-columns: 1fr;
            gap: 0.75rem;
        }
    }

    @media (max-width: 640px) {
        .block-container { padding: 1.45rem 0.85rem 3.5rem; }
        .cf-page-title { font-size: 2rem; }
        .cf-page-description { font-size: 0.93rem; }
        .cf-balance-hero { padding: 1.2rem; }
        .cf-balance-value { font-size: 2rem; }
        .cf-feature-card { min-height: auto; margin-bottom: 0.65rem; }
    }
</style>
"""


def apply_app_styles() -> None:
    """Install the static, privacy-safe application stylesheet."""
    st.markdown(APP_STYLES, unsafe_allow_html=True)


__all__ = ["APP_STYLES", "apply_app_styles"]
