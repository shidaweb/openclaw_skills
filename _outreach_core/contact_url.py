"""Contact form URL candidate and form-type classification helpers (v8, v15 §F)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlparse


_CONTACT_TEXT_KW = (
    "お問い合わせ",
    "お問合せ",
    "お問い合せ",
    "企業のお問い合わせ",
    "法人のお問い合わせ",
    "取材",
    "協業",
    "提携",
    "contact",
    "inquiry",
)
_EXCLUDE_TEXT_KW = (
    "採用",
    "recruit",
    "career",
    "求人",
    "応募",
    "entry",
    "投資家",
    "faq",
    "よくある質問",
    "予約",
    "reserve",
    "support",
    "サポート",
    "login",
    "ログイン",
)
_DESIRED_PATH_KW = (
    "/contact",
    "/inquiry",
    "/toiawase",
    "/otoiawase",
    "/company/contact",
    "/business",
    "/form",
)
# v31 §WS2c — the avoid list is split in two. Hard-avoid paths (recruit/IR/
# reservation) are never contact forms even when the path also says contact
# (/recruit/contact is an applicant form). Soft-avoid paths ARE legitimately
# where many JP sites put the B2B inquiry (/support/contact, /support/inquiry),
# so a desired keyword in the path overrides them.
_HARD_AVOID_PATH_KW = (
    "/recruit",
    "/career",
    "/saiyo",
    "/entry",
    "/ir",
    "/reserve",
    "/yoyaku",
)
_SOFT_AVOID_PATH_KW = (
    "/support",
    "/faq",
)
_AVOID_PATH_KW = _HARD_AVOID_PATH_KW + _SOFT_AVOID_PATH_KW

_NON_CONTACT_HEADING_KW = {
    "register": ("会員登録", "新規登録", "アカウント作成", "アカウント登録", "ユーザー登録"),
    "login": ("ログイン",),
    "recruit": ("採用", "応募", "求人", "履歴書", "エントリーシート", "ES提出", "中途採用", "新卒採用"),
    "reservation": ("予約フォーム", "来店予約", "ご予約", "施術予約", "見学予約"),
    "estimate_consumer": ("無料相談", "無料カウンセリング", "施術に関する相談"),
    "ir": ("IR", "投資家情報", "株主", "適時開示"),
    "b2c_support": ("お客様相談室", "カスタマーサポート", "修理", "返品"),
    "document_request": ("資料請求", "資料ダウンロード"),
}
_B2B_HINT_KW = ("法人", "取引", "協業", "提携", "ビジネス", "企業")
_RECRUIT_FIELD_KW = (
    "応募", "エントリー", "履歴書", "職務経歴", "志望動機", "希望職種", "希望勤務地",
    "applicant", "resume", "entry", "career",
)
_CONTACT_TEXTAREA_KW = ("お問い合わせ", "問合せ", "本文", "ご相談", "内容", "メッセージ", "message", "inquiry")
_SEARCH_TEXTAREA_KW = ("検索", "search", "keyword", "query")
_PRE_FORM_GATE_KW = (
    "メールフォームはこちら",
    "お問い合わせフォームはこちら",
    "お問い合わせ種別",
    "上記に同意してお問い合わせする",
    "同意してお問い合わせ",
)
_ERROR_PAGE_KW = (
    "not found",
    "page not found",
    "ページが見つかりません",
    "お探しのページ",
    "存在しません",
    "forbidden",
    "アクセスできません",
    "アクセスが禁止",
    "server error",
)
# v31 §WS2d — bare status codes moved out of the substring keywords: 「従業員
# 500名」「1500円」 in body copy used to flag a healthy page as an error.
# \b keeps 500名/1500円 unmatched (kanji are \w so no boundary forms), and the
# match is restricted to the snapshot head where aria snapshots put the
# title/heading of a real error page.
_ERROR_CODE_RE = re.compile(r"\b(?:404|500|503)\b")
_ERROR_CODE_HEAD_CHARS = 400

# v25: email-verification-code (OTP) gate detection. Forms like フェリシモ
# require "メールに確認コード(6桁)を送信→入力" before the message can be
# submitted; the pipeline cannot receive that email, so the confirm step
# silently resets and surfaces as "form disappeared". Detect the gate upfront.
_OTP_TEXT_KW = (
    "確認コード",
    "認証コード",
    "認証番号",
    "確認番号",
    "ワンタイムパスワード",
    "ワンタイムコード",
    "verification code",
    "one-time password",
    "one time password",
    "コードを送信",
    "コードをお送り",
    "コードを入力",
    "6ケタの数字",
    "6桁の数字",
    "6桁のコード",
    "メールに記載されたコード",
)
_OTP_FIELD_KW = (
    "確認コード",
    "認証コード",
    "認証番号",
    "otp",
    "verification_code",
    "verificationcode",
    "auth_code",
    "authcode",
    "onetime",
    "one_time",
)


def registrable_domain(url_or_host: str) -> str:
    host = _host_only(url_or_host)
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    suffix2 = ".".join(parts[-2:])
    jp_second_level = {"co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp", "ed.jp", "gr.jp", "lg.jp"}
    if suffix2 in jp_second_level and len(parts) >= 3:
        return ".".join(parts[-3:])
    return suffix2


def same_registrable_domain(url_a: str, url_b: str) -> bool:
    da = registrable_domain(url_a)
    db = registrable_domain(url_b)
    return bool(da and db and da == db)


def absolutize_url(href: str, base_url: str) -> str:
    if not href:
        return ""
    return _normalize_http_url(urljoin(base_url, href.strip()))


def contact_link_candidates(page_links: list[dict[str, str]], base_url: str) -> list[str]:
    scored: list[tuple[int, str]] = []
    for row in page_links or []:
        href = str((row or {}).get("href") or "").strip()
        txt = str((row or {}).get("text") or "").strip()
        abs_url = absolutize_url(href, base_url)
        if not abs_url:
            continue
        if not same_registrable_domain(abs_url, base_url):
            continue
        # v31 §WS2c — when the path itself says contact/inquiry, judge the
        # exclude keywords on the link TEXT only: the URL half of the blob
        # otherwise re-drops /support/contact via the "support" keyword that
        # _path_should_avoid just deliberately overrode. Hard-avoid paths
        # (/recruit/contact …) are still rejected below regardless.
        low_url = abs_url.lower()
        has_desired = any(kw in low_url for kw in _DESIRED_PATH_KW)
        excl_blob = txt.lower() if has_desired else f"{txt} {abs_url}".lower()
        if any(kw.lower() in excl_blob for kw in _EXCLUDE_TEXT_KW):
            continue
        if _path_should_avoid(abs_url):
            continue

        score = 0
        if any(kw.lower() in txt.lower() for kw in _CONTACT_TEXT_KW):
            score += 3
        if any(kw in abs_url.lower() for kw in _DESIRED_PATH_KW):
            score += 2
        if score <= 0:
            continue
        scored.append((score, abs_url))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return _dedupe_urls([u for _s, u in scored])


def _path_should_avoid(url: str) -> bool:
    """v31 §WS2c — avoid-list check with soft-avoid override.

    Hard-avoid paths (recruit/IR/…) always disqualify — /recruit/contact is
    an applicant form no matter what the path also says. Soft-avoid paths
    (support/faq) are overridden by a desired keyword because many JP sites
    put the B2B inquiry under /support/contact or /support/inquiry.
    """
    low = (url or "").lower()
    if any(kw in low for kw in _HARD_AVOID_PATH_KW):
        return True
    if not any(kw in low for kw in _SOFT_AVOID_PATH_KW):
        return False
    return not any(kw in low for kw in _DESIRED_PATH_KW)


def common_contact_paths(base_url: str) -> list[str]:
    p = urlparse(base_url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return []
    root = f"{p.scheme}://{p.netloc}"
    paths = (
        "/contact",
        "/contact/",
        "/inquiry",
        "/toiawase",
        "/otoiawase",
        "/company/contact",
        "/business/contact",
        "/form",
        # v15 §F2 expansion
        "/contact-us",
        "/contactus",
        "/inquiry/",
        "/contact/business",
        "/business/inquiry",
        "/company/inquiry",
        "/support/inquiry",
        "/お問い合わせ",
        "/%E3%81%8A%E5%95%8F%E3%81%84%E5%90%88%E3%82%8F%E3%81%9B",
        # v31 §WS2a expansion — common JP patterns observed missing in
        # production (bare "/mail" deliberately excluded: webmail false hits).
        "/contactform",
        "/contact.html",
        "/inquiry.html",
        "/otoiawase.html",
        "/support/contact",
        "/ja/contact",
        "/mailform",
    )
    return _dedupe_urls([f"{root}{x}" for x in paths])


# --- v15 §F2: sitemap.xml mining -------------------------------------------
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
# v31 §WS2a — ``mailform`` and slash-anchored ``/form`` added. The anchor
# matters: a bare ``form`` alternative would match /reform and /information.
_SITEMAP_CONTACT_RE = re.compile(
    r"contact|inquiry|toiawase|otoiawase|mailform|/form(?:[/.?]|$)",
    re.IGNORECASE,
)


def extract_contact_urls_from_sitemap(xml_text: str | None) -> list[str]:
    """Contact-looking URLs from a sitemap.xml body (pure; tolerant of junk).

    Filters out recruit/IR-style paths with the same avoid-list used for link
    candidates, normalizes, and dedupes preserving order.
    """
    if not xml_text:
        return []
    out: list[str] = []
    for m in _SITEMAP_LOC_RE.finditer(xml_text):
        url = (m.group(1) or "").strip()
        if not url or not _SITEMAP_CONTACT_RE.search(url):
            continue
        if _path_should_avoid(url):
            continue
        out.append(url)
    return _dedupe_urls(out)


# --- v15 §F3: iframe / hosted form services ---------------------------------
#
# v30 §WS-B adds hosts observed in production logs (LegalOn 2026-06-29) that
# were silently missed because the parent page and the iframe host live on
# different registrable domains.
KNOWN_FORM_SERVICE_HOSTS = (
    "form.run",
    "formrun",
    "tayori.com",
    "hsforms.com",
    "hubspot",
    "kintoneapp.com",
    "formzu",
    "secure-link",
    "synergy",
    "formok",
    "ssl-form",
    "formmailer",
    "marketo.com",
    "salesforce.com",
    # v30 §WS-B — LegalOn's hosted form embedded inside legalontech.jp/contact/.
    "legalforce-cloud.com",
    "mktoweb.com",
    "pardot.com",
    # v31 §WS2b — Google Forms short links. Full docs.google.com embeds are
    # accepted only when the src path contains /forms (see is_form_service_url
    # / iframe_form_src) so an arbitrary Google Docs embed doesn't qualify.
    "forms.gle",
)


def is_form_service_url(url: str) -> bool:
    """v31 §WS2b — does ``url`` live on a hosted-form service?

    Shared by iframe takeover and bootstrap dedup (different companies'
    forms on the same service must not collapse to one registrable domain).
    """
    host = _host_only(url)
    if not host:
        return False
    if any(svc in host for svc in KNOWN_FORM_SERVICE_HOSTS):
        return True
    return "docs.google.com" in host and "/forms" in urlparse(url).path.lower()


def iframe_form_src(
    iframes: list[dict[str, Any]] | None, base_url: str
) -> str | None:
    """If one of the page's iframes hosts a real form, return its src to re-enrich.

    Accept when the iframe host is a known hosted-form service OR shares the
    page's registrable domain. Returns the first acceptable src, else None.
    """
    for fr in iframes or []:
        src = str((fr or {}).get("src") or "").strip()
        if not src:
            continue
        host = _host_only(src)
        if not host:
            continue
        if is_form_service_url(src):
            return src
        if base_url and same_registrable_domain(src, base_url):
            return src
    return None


# --- v15 §F1: two-stage classification (heuristics → LLM only when uncertain)
_VALID_LLM_FORM_TYPES = (
    "contact", "recruit", "login", "b2c_support", "ir", "reservation", "other",
)


def classification_is_uncertain(
    kind: str, reason: str | None, fields: dict | None
) -> bool:
    """Is the heuristic classification ambiguous enough to ask the LLM? (§F1)

    Uncertain when:
      - kind is ``unknown_no_textarea`` (the dominant mis-skip bucket), or
      - reason is heading-based ("heading mentions ...") yet a textarea exists
        (heading said non-contact but the form looks like one), or
      - zero fields were collected at all (likely JS-rendered or wrong root).
    """
    f = fields or {}
    field_count = (
        len(f.get("inputs") or [])
        + len(f.get("selects") or [])
        + len(f.get("textareas") or [])
    )
    if kind == "unknown_no_textarea":
        return True
    if (reason or "").startswith("heading mentions") and (f.get("textareas") or []):
        return True
    if field_count == 0:
        return True
    return False


def _llm_classify_prompt(snapshot: str | None, fields: dict | None) -> str:
    f = fields or {}
    field_summary = {
        "inputs": [
            {"name": i.get("name"), "label": i.get("label"), "type": i.get("type")}
            for i in (f.get("inputs") or [])[:25]
        ],
        "selects": [
            {"name": s.get("name"), "label": s.get("label")}
            for s in (f.get("selects") or [])[:10]
        ],
        "textareas": [
            {"name": t.get("name"), "label": t.get("label")}
            for t in (f.get("textareas") or [])[:5]
        ],
    }
    return (
        "You are classifying a Japanese corporate web page that may contain a form.\n"
        "Decide what kind of form (if any) this page is for.\n\n"
        "## Page snapshot (head)\n"
        f"{(snapshot or '')[:4000]}\n\n"
        "## Collected form fields\n"
        f"{json.dumps(field_summary, ensure_ascii=False)}\n\n"
        "## Output — STRICT JSON only, no prose\n"
        '{"form_type": "contact|recruit|login|b2c_support|ir|reservation|other",\n'
        ' "confidence": 0.0-1.0,\n'
        ' "b2b_contact_hint_url": "<url of a 法人/business contact link visible'
        " on this page, or null>\"}\n"
        "Rules: form_type=contact means a general/B2B inquiry form a vendor may "
        "legitimately use. Recruit/entry, login, member registration, consumer "
        "support, IR and reservation forms are NOT contact.\n"
    )


def parse_llm_classification(raw: str | None) -> dict[str, Any] | None:
    """Parse + validate the LLM classification JSON. None when unusable (pure)."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    form_type = str(data.get("form_type") or "").strip().lower()
    if form_type not in _VALID_LLM_FORM_TYPES:
        return None
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        return None
    confidence = max(0.0, min(1.0, confidence))
    hint = data.get("b2b_contact_hint_url")
    hint_url = str(hint).strip() if isinstance(hint, str) and hint.strip() else None
    if hint_url and not hint_url.lower().startswith(("http://", "https://")):
        hint_url = None
    return {"form_type": form_type, "confidence": confidence,
            "b2b_contact_hint_url": hint_url}


def _llm_escalate_classification(
    kind: str,
    reason: str | None,
    fields: dict,
    snapshot: str | None,
    *,
    infer_fn: Callable[[str, str], str | None] | None = None,
    model: str = "",
    min_confidence: float = 0.6,
) -> dict[str, Any]:
    """Apply the LLM second stage to an existing heuristic verdict (§F1).

    Separated from ``classify_form_type_v2`` so callers (e.g. enrich in run.py)
    can supply a mockable heuristic hook while still getting LLM escalation.
    """
    out: dict[str, Any] = {
        "kind": kind, "reason": reason, "src": "heuristic",
        "llm_called": False, "b2b_contact_hint_url": None,
    }
    if not infer_fn or not classification_is_uncertain(kind, reason, fields):
        return out
    out["llm_called"] = True
    try:
        raw = infer_fn(_llm_classify_prompt(snapshot, fields), model)
    except Exception:
        return out
    parsed = parse_llm_classification(raw)
    if not parsed:
        return out
    out["b2b_contact_hint_url"] = parsed.get("b2b_contact_hint_url")
    if parsed["confidence"] < min_confidence:
        return out
    llm_kind = parsed["form_type"]
    # "other" carries no actionable signal — keep heuristic verdict.
    if llm_kind == "other":
        return out
    out["kind"] = llm_kind
    out["reason"] = f"llm:{llm_kind} (conf={parsed['confidence']:.2f})"
    out["src"] = "llm"
    return out


def classify_form_type_v2(
    fields: dict,
    snapshot: str | None,
    *,
    infer_fn: Callable[[str, str], str | None] | None = None,
    model: str = "",
    min_confidence: float = 0.6,
) -> dict[str, Any]:
    """Two-stage classification (§F1): heuristics first, LLM only when uncertain.

    Returns {"kind", "reason", "src": "heuristic"|"llm", "llm_called": bool,
    "b2b_contact_hint_url": str|None}. The LLM is never the final arbiter when
    its confidence is below ``min_confidence`` and any failure (no infer_fn,
    timeout, parse error) falls back to the heuristic result.
    """
    kind, reason = classify_form_type(fields, snapshot)
    return _llm_escalate_classification(
        kind, reason, fields, snapshot,
        infer_fn=infer_fn, model=model, min_confidence=min_confidence,
    )


def classify_form_type(fields: dict, snapshot: str | None) -> tuple[str, str | None]:
    """Heuristic form classifier driven by an ordered policy table.

    Each policy is ``(check_fn) -> (kind, reason) | None``. The first policy
    that returns a verdict wins. Policies are arranged from most-specific
    (password field → register) to most-general (textarea + submit → contact).

    v30 §WS-B refactor preserves the exact behaviour of the prior nested
    if/else version (locked by ``test_contact_url*``) — the difference is
    purely organizational: each rule now lives in a named function so a
    failure can be attributed to the first rule that fired, and new rules
    slot in by adding a row to ``_POLICY`` rather than threading another
    branch through the nest.
    """
    inputs = fields.get("inputs") or []
    textareas = fields.get("textareas") or []
    snap_head = (snapshot or "")[:3000]
    name_blob = " ".join(
        str(x.get("name") or x.get("label") or "") for x in inputs
    ).lower()
    recruit_field_hit = any(k.lower() in name_blob for k in _RECRUIT_FIELD_KW)
    ctx = _ClassifyCtx(
        fields=fields,
        inputs=inputs,
        textareas=textareas,
        snap_head=snap_head,
        name_blob=name_blob,
        recruit_field_hit=recruit_field_hit,
    )
    for policy in _CLASSIFY_POLICIES:
        verdict = policy(ctx)
        if verdict is not None:
            return verdict
    return ("contact", None)


@dataclass
class _ClassifyCtx:
    """Pre-computed inputs shared by every policy in :data:`_CLASSIFY_POLICIES`."""

    fields: dict
    inputs: list
    textareas: list
    snap_head: str
    name_blob: str
    recruit_field_hit: bool


def _policy_password_field(ctx: _ClassifyCtx) -> tuple[str, str] | None:
    """Any password field → registration form, regardless of other signals."""
    for inp in ctx.inputs:
        if str(inp.get("type", "")).lower() == "password":
            return ("register", "password field present")
    return None


def _policy_birthdate_field(ctx: _ClassifyCtx) -> tuple[str, str] | None:
    """A birth-date field (birth + year combo, or 生年月日) is strongly
    register-indicating — contact forms never ask for it."""
    if ("birth" in ctx.name_blob and "year" in ctx.name_blob) or (
        "生年月日" in ctx.name_blob
    ):
        return ("register", "birth-date field present")
    return None


def _policy_recruit_strong(ctx: _ClassifyCtx) -> tuple[str, str] | None:
    """A recruit heading AND applicant fields → recruit (even with textarea).
    A textarea alone on /careers/ would otherwise misclassify as contact."""
    if (
        any(k in ctx.snap_head for k in _NON_CONTACT_HEADING_KW["recruit"])
        and ctx.recruit_field_hit
    ):
        return ("recruit", "recruit heading + applicant fields")
    return None


def _policy_non_contact_headings(ctx: _ClassifyCtx) -> tuple[str, str] | None:
    """Heading-based classification with B2B escape hatch for IR / b2c_support
    / document_request. Recruit also lives here but only fires when applicant
    fields are present or no textarea exists (a bare 採用 link in the global
    nav must not poison a real /contact/ page)."""
    for kind, kws in _NON_CONTACT_HEADING_KW.items():
        if not any(kw in ctx.snap_head for kw in kws):
            continue
        if kind in ("ir", "b2c_support", "document_request"):
            if any(h in ctx.snap_head for h in _B2B_HINT_KW):
                continue
            return (kind, f"heading mentions {kind}")
        if kind == "recruit":
            if ctx.recruit_field_hit or not ctx.textareas:
                return (kind, "recruit heading detected")
            continue
        if not ctx.textareas:
            return (kind, f"heading mentions {kind}")
    return None


def _policy_pre_form_gate(ctx: _ClassifyCtx) -> tuple[str, str] | None:
    """A pre-form gate page (no textarea yet, but the page invites a contact
    inquiry via radio / agreement checkbox) is still the right contact flow."""
    if _looks_like_contact_gate(ctx.fields, ctx.snap_head):
        return ("contact", "pre_form_gate")
    return None


def _policy_textarea_plus_submit(ctx: _ClassifyCtx) -> tuple[str, str] | None:
    """The canonical contact form: an inquiry textarea + a submit control."""
    if _has_contact_textarea(ctx.fields) and _has_submit_control(
        ctx.fields, ctx.snap_head
    ):
        return ("contact", "textarea_plus_submit")
    return None


def _policy_no_textarea(ctx: _ClassifyCtx) -> tuple[str, str] | None:
    """No inquiry textarea (and not a pre-form gate) → cannot send."""
    if not _has_contact_textarea(ctx.fields):
        return ("unknown_no_textarea", "no valid inquiry textarea")
    return None


_CLASSIFY_POLICIES = (
    _policy_password_field,
    _policy_birthdate_field,
    _policy_recruit_strong,
    _policy_non_contact_headings,
    _policy_pre_form_gate,
    _policy_textarea_plus_submit,
    _policy_no_textarea,
)


def is_error_page(snapshot: str | None, url: str | None = None, http_status: int | None = None) -> bool:
    if isinstance(http_status, int) and 400 <= http_status <= 599:
        return True
    snap = (snapshot or "").lower()
    if any(k in snap for k in _ERROR_PAGE_KW):
        return True
    # v31 §WS2d — bare 404/500/503 only count near the top of the snapshot
    # (error pages lead with their code; body copy mentions of 500 don't).
    if _ERROR_CODE_RE.search(snap[:_ERROR_CODE_HEAD_CHARS]):
        return True
    u = (url or "").lower()
    if any(x in u for x in ("/404", "error", "notfound")):
        if "inquiry" not in u and "contact" not in u:
            return True
    return False


def detect_email_verification(
    snapshot: str | None, fields: dict | None = None
) -> dict[str, Any]:
    """v25: detect an email-verification-code (OTP) gate on the current page.

    Returns ``{"detected": bool, "evidence": [matched keywords / field names]}``.
    Text evidence alone requires 2+ distinct keyword hits OR 1 keyword combined
    with a mail-send phrase, to avoid false positives on pages that merely
    mention 確認 in passing. A dedicated code input field counts as strong
    evidence on its own.
    """
    evidence: list[str] = []
    snap = snapshot or ""
    text_hits = [k for k in _OTP_TEXT_KW if k in snap or k in snap.lower()]
    evidence.extend(f"text:{k}" for k in text_hits)

    field_hits: list[str] = []
    f = fields if isinstance(fields, dict) else {}
    for inp in f.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        blob = " ".join(
            str(inp.get(k) or "") for k in ("name", "label", "placeholder", "id")
        ).lower()
        if any(k in blob for k in _OTP_FIELD_KW):
            field_hits.append(str(inp.get("name") or inp.get("label") or "code"))
    evidence.extend(f"field:{n}" for n in field_hits)

    send_phrase = any(
        p in snap for p in ("送信します", "お送りします", "メールを送信", "メールでお送り")
    )
    detected = bool(field_hits) or len(text_hits) >= 2 or (
        len(text_hits) == 1 and send_phrase
    )
    return {"detected": detected, "evidence": evidence[:8]}


def classify_page_form_state(fields: dict | None, snapshot: str | None) -> dict:
    """Send-time page vetting (v17): does the CURRENT page actually carry a form?

    Production showed "first submit button not found / 0 candidates" cases whose
    real cause was the URL itself: a redirect to a different site (ain_holdings),
    a contact GUIDE page (links to forms, no form), or an expired/bounced session
    page (duskin). Those must be caught BEFORE fill/submit and reported as a URL
    problem, not a button problem.

    Returns {"state": ..., counts...} where state is one of:
      - "form_ok":      fillable controls present → proceed
      - "gate_like":    no textarea but radios/checkbox/button (pre-form gate or
                        wizard step) → proceed (gate logic handles it)
      - "no_form":      page rendered but has no form controls → URL problem
      - "error_page":   404/error keywords → URL problem
      - "empty_render": no controls AND nearly empty body → likely JS still
                        rendering → caller should wait + rescan once
    """
    f = fields if isinstance(fields, dict) else {}
    inputs = [
        x for x in (f.get("inputs") or [])
        if str((x or {}).get("type") or "").lower()
        not in ("hidden", "submit", "button", "image")
    ]
    textareas = f.get("textareas") or []
    buttons = f.get("submit_buttons") or []
    radios = f.get("radios") or {}
    radio_n = len(radios) if isinstance(radios, (dict, list)) else 0
    checks = f.get("checkboxes") or []
    counts = {
        "inputs": len(inputs),
        "textareas": len(textareas),
        "submit_buttons": len(buttons),
        "radio_groups": radio_n,
        "checkboxes": len(checks),
    }
    snap = snapshot or ""
    if is_error_page(snap):
        return {"state": "error_page", **counts}
    controls = counts["inputs"] + counts["textareas"] + radio_n + counts["checkboxes"]
    if controls == 0:
        state = "empty_render" if len(snap.strip()) < 600 else "no_form"
        return {"state": state, **counts}
    if counts["textareas"] == 0 and counts["inputs"] <= 1:
        return {"state": "gate_like", **counts}
    return {"state": "form_ok", **counts}


def _looks_like_contact_gate(fields: dict, snap_head: str) -> bool:
    radios = fields.get("radios") or {}
    checks = fields.get("checkboxes") or []
    has_radio = bool(radios)
    has_agreement_checkbox = any("同意" in str(c.get("label") or "") for c in checks if isinstance(c, dict))
    text_hit = any(k in snap_head for k in _PRE_FORM_GATE_KW)
    contact_heading = ("お問い合わせ" in snap_head) or ("contact" in snap_head.lower())
    return contact_heading and text_hit and (has_radio or has_agreement_checkbox)


def _has_contact_textarea(fields: dict) -> bool:
    textareas = fields.get("textareas") or []
    inputs = fields.get("inputs") or []
    if not textareas:
        return False
    for ta in textareas:
        blob = f"{ta.get('name','')} {ta.get('label','')} {ta.get('placeholder','')}".lower()
        if any(k in blob for k in _SEARCH_TEXTAREA_KW):
            continue
        if any(k in blob for k in _CONTACT_TEXTAREA_KW):
            return True
    # fallback: if there is at least one non-search textarea in a standard contact-like form
    non_search = []
    for ta in textareas:
        blob = f"{ta.get('name','')} {ta.get('label','')} {ta.get('placeholder','')}".lower()
        if any(k in blob for k in _SEARCH_TEXTAREA_KW):
            continue
        non_search.append(ta)
    if not non_search:
        return False
    inp_blob = " ".join(str(i.get("name") or i.get("label") or "") for i in inputs).lower()
    return ("mail" in inp_blob or "email" in inp_blob or "氏名" in inp_blob or "name" in inp_blob)


def _has_submit_control(fields: dict, snap_head: str) -> bool:
    for btn in fields.get("submit_buttons") or []:
        if not isinstance(btn, dict):
            continue
        text = str(btn.get("text") or "").lower()
        if any(k in text for k in ("送信", "submit", "確認", "完了", "確定")):
            return True
    return any(k in snap_head.lower() for k in ("送信", "submit", "確認", "完了", "確定"))


def _normalize_http_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}{p.path or ''}"


def _host_only(url_or_host: str) -> str:
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    if "://" not in raw:
        return raw.split("/")[0]
    p = urlparse(raw)
    return (p.hostname or "").lower()


def _dedupe_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        x = _normalize_http_url(u)
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out
