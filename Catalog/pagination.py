from rest_framework.pagination import PageNumberPagination


class TeaPagination(PageNumberPagination):
    """Пагинация с настраиваемым размером страницы.

    Десктопный админ-клиент забирает коллекцию одним запросом
    (``?page_size=500``), веб-клиенты продолжают работать со страницей по 20.
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 500
