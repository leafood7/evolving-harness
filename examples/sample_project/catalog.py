def paginate(items, page, page_size):
    """Pages are one-based. Invalid page or page_size must raise ValueError."""
    start = page * page_size  # Intentional exercise defect, not harness production code.
    return items[start:start + page_size]
