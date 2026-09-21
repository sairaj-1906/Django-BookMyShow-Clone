from django.urls import path

from . import views

urlpatterns = [
    path("", views.movie_list, name="movie_list"),
    path("<int:movie_id>/", views.movie_detail, name="movie_detail"),
    path("<int:movie_id>/theaters/", views.show_list, name="theater_list"),
    path("<int:movie_id>/reviews/submit/", views.submit_review, name="submit_review"),
    path("reviews/<int:review_id>/edit/", views.edit_review, name="edit_review"),
    path("reviews/<int:review_id>/delete/", views.delete_review, name="delete_review"),
    path("reviews/<int:review_id>/report/", views.report_review, name="report_review"),
    path("shows/<int:show_id>/seats/book/", views.book_seats, name="book_seats"),
    path(
        "theater/<int:theater_id>/seats/book/",
        views.book_seats_legacy,
        name="book_seats_legacy",
    ),
]
