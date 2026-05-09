from urllib.parse import urlsplit


ALLOWED_BACK_LABELS = {
    "К аналитике",
    "К графику",
    "К группам",
    "К заявкам",
    "К заявке",
    "К переносу",
    "К планированию",
    "К отделам",
    "К правилам состава",
    "К профилю",
    "К сбору",
    "К черновику",
    "К сотрудникам",
    "К сотруднику",
    "К уведомлениям",
}


def get_safe_return_path(value):
    if not value or not isinstance(value, str):
        return ""

    value = value.strip()
    if not value.startswith("/") or value.startswith("//"):
        return ""

    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return ""

    return value


def build_explicit_back_link(query_params, section=""):
    back_url = get_safe_return_path(query_params.get("back_url", ""))
    back_label = query_params.get("back_label", "")
    if not back_url or back_label not in ALLOWED_BACK_LABELS:
        return None

    return {
        "label": back_label,
        "url": back_url,
        "section": section,
        "use_remembered_list": False,
    }
