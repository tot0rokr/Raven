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
    path("api/sessions/<str:session_id>/pause/", views.pause_session),
    path("api/sessions/<str:session_id>/resume/", views.resume_session),
    path("api/sessions/<str:session_id>/", views.delete_session),
    path("api/files/", views.files),
    path("api/files/uploads/", views.start_upload),
    path("api/files/uploads/<str:token>/", views.upload),
    path("api/files/uploads/<str:token>/finish/", views.finish_upload),
    path("api/files/download/", views.download),
    path("api/certificate/", views.certificate),
    path("api/certificate/download/", views.certificate_download),
]
