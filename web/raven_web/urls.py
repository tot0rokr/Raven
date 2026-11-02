from django.urls import path

from . import views

urlpatterns = [
    path("", views.index),
    path("login/", views.login_view),
    path("logout/", views.logout_view),
    path("api/sessions/", views.sessions),
    path("api/sessions/<str:session_id>/open/", views.open_session),
    path("api/sessions/<str:session_id>/rename/", views.rename_session),
    path("api/sessions/<str:session_id>/run/", views.run_session),
    path("api/sessions/<str:session_id>/", views.delete_session),
]
