from django.contrib import admin
from django.db import connection
from django.db.utils import OperationalError
from django.http import JsonResponse
from django.urls import path
from ninja import NinjaAPI

from blog.api import router as blog_router

api = NinjaAPI()
api.add_router("/", blog_router)


def health(request):
    try:
        connection.ensure_connection()
    except OperationalError:
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("health", health),
]
