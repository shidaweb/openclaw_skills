"""Contact form URL candidate and form-type classification helpers (v8)."""

from __future__ import annotations

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
_AVOID_PATH_KW = (
    "/recruit",
    "/career",
    "/saiyo",
    "/entry",
    "/ir",
    "/support",
    "/faq",
    "/reserve",
    "/yoyaku",
)

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
        blob = f"{txt} {abs_url}".lower()
        if any(kw.lower() in blob for kw in _EXCLUDE_TEXT_KW):
            continue
        if any(kw in abs_url.lower() for kw in _AVOID_PATH_KW):
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
    )
    return _dedupe_urls([f"{root}{x}" for x in paths])


def classify_form_type(fields: dict, snapshot: str | None) -> tuple[str, str | None]:
    inputs = fields.get("inputs") or []
    textareas = fields.get("textareas") or []
    snap_head = (snapshot or "")[:3000]

    # Strong register signal
    for inp in inputs:
        if str(inp.get("type", "")).lower() == "password":
            return ("register", "password field present")

    name_blob = " ".join(str(x.get("name") or x.get("label") or "") for x in inputs).lower()
    if ("birth" in name_blob and "year" in name_blob) or ("生年月日" in name_blob):
        return ("register", "birth-date field present")

    # Recruit should stay non-contact even with textarea when strong signals exist.
    recruit_field_hit = any(k.lower() in name_blob for k in _RECRUIT_FIELD_KW)
    if any(k in snap_head for k in _NON_CONTACT_HEADING_KW["recruit"]) and recruit_field_hit:
        return ("recruit", "recruit heading + applicant fields")

    # Heading-based classification with B2B escape hatch for B2C/IR-like pages.
    for kind, kws in _NON_CONTACT_HEADING_KW.items():
        if any(kw in snap_head for kw in kws):
            if kind in ("ir", "b2c_support", "document_request"):
                if any(h in snap_head for h in _B2B_HINT_KW):
                    continue
                return (kind, f"heading mentions {kind}")
            if kind == "recruit":
                return (kind, "recruit heading detected")
            if not textareas:
                return (kind, f"heading mentions {kind}")

    # Pre-form gate page: no textarea yet, but this is still the right contact flow.
    if _looks_like_contact_gate(fields, snap_head):
        return ("contact", "pre_form_gate")

    if not _has_contact_textarea(fields):
        return ("unknown_no_textarea", "no valid inquiry textarea")

    return ("contact", None)


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
