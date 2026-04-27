from django import template

register = template.Library()

@register.filter
def chunked(value, chunk_size):
    """Yield successive chunk_size-sized chunks from value."""
    chunk_size = int(chunk_size)
    return [value[i:i + chunk_size] for i in range(0, len(value), chunk_size)]