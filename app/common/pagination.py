from sqlalchemy import Select


def paginate(query: Select, page: int, page_size: int) -> Select:
    """Apply offset/limit pagination. Pages and page sizes below 1 are clamped to 1."""
    safe_page = max(page, 1)
    safe_page_size = max(page_size, 1)
    offset = (safe_page - 1) * safe_page_size
    return query.offset(offset).limit(safe_page_size)
