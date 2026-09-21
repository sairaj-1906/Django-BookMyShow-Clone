from django.contrib import admin

from .models import (
    Booking,
    CastMember,
    Genre,
    Language,
    Movie,
    MovieCastCredit,
    MoviePoster,
    Review,
    ReviewReport,
    Seat,
    Show,
    Theater,
)


class MoviePosterInline(admin.TabularInline):
    model = MoviePoster
    extra = 1


class MovieCastCreditInline(admin.TabularInline):
    model = MovieCastCredit
    extra = 1
    autocomplete_fields = ["cast_member"]


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    search_fields = ["name"]


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ["name", "code"]
    search_fields = ["name", "code"]


@admin.register(CastMember)
class CastMemberAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "age_certification",
        "duration_minutes",
        "release_date",
        "display_rating",
        "ratings_count",
    ]
    list_filter = ["age_certification", "genres", "languages", "release_date"]
    search_fields = ["name", "description", "cast"]
    filter_horizontal = ["genres", "languages"]
    readonly_fields = ["average_rating", "ratings_count"]
    inlines = [MoviePosterInline, MovieCastCreditInline]
    fieldsets = (
        (None, {"fields": ("name", "tagline", "image", "description")}),
        (
            "Details",
            {
                "fields": (
                    "duration_minutes",
                    "age_certification",
                    "release_date",
                    "genres",
                    "languages",
                )
            },
        ),
        ("Trailer", {"fields": ("trailer_youtube_id",)}),
        ("Cast (legacy free text, optional)", {"fields": ("cast",)}),
        ("Ratings", {"fields": ("rating", "average_rating", "ratings_count")}),
    )

    def display_rating(self, obj):
        return obj.display_rating

    display_rating.short_description = "Rating"


@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "address"]
    search_fields = ["name", "city"]


@admin.register(Show)
class ShowAdmin(admin.ModelAdmin):
    list_display = [
        "movie",
        "theater",
        "language",
        "screen_format",
        "date_time",
        "price",
    ]
    list_filter = ["screen_format", "language", "theater"]
    autocomplete_fields = ["movie", "theater"]
    date_hierarchy = "date_time"


@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ["show", "theater", "seat_number", "is_booked"]
    list_filter = ["is_booked"]


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["user", "seat", "movie", "show", "booked_at"]
    list_filter = ["movie"]


class ReviewReportInline(admin.TabularInline):
    model = ReviewReport
    extra = 0
    readonly_fields = ["reported_by", "reason", "comment", "created_at"]
    can_delete = False


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = [
        "movie",
        "user",
        "rating",
        "is_verified_viewer",
        "is_hidden",
        "report_count",
        "created_at",
    ]
    list_filter = ["is_verified_viewer", "is_hidden", "rating"]
    search_fields = ["movie__name", "user__username", "body"]
    inlines = [ReviewReportInline]
    actions = ["hide_reviews", "unhide_reviews"]

    def hide_reviews(self, request, queryset):
        for review in queryset:
            review.is_hidden = True
            review.save()

    hide_reviews.short_description = "Hide selected reviews"

    def unhide_reviews(self, request, queryset):
        for review in queryset:
            review.is_hidden = False
            review.save()

    unhide_reviews.short_description = "Unhide selected reviews"


@admin.register(ReviewReport)
class ReviewReportAdmin(admin.ModelAdmin):
    list_display = ["review", "reported_by", "reason", "created_at"]
    list_filter = ["reason"]
