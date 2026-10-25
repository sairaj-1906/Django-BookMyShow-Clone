from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from .models import (
    Booking,
    Cancellation,
    CastMember,
    Genre,
    Language,
    Movie,
    MovieCastCredit,
    MoviePoster,
    MovieView,
    Payment,
    Review,
    ReviewReport,
    Seat,
    SeatHold,
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
    list_display = ["user", "seat", "movie", "show", "payment", "booked_at"]
    list_filter = ["movie"]
    actions = ["cancel_and_refund"]

    @admin.action(description="Cancel selected bookings and create refund records")
    def cancel_and_refund(self, request, queryset):
        """Free the seat, delete the booking and record a Cancellation (refund pending).
        Only upcoming shows can be cancelled. The refund itself is then marked processed
        from the Cancellations admin once the money has actually been returned."""
        done = skipped = 0
        with transaction.atomic():
            for booking in queryset.select_related("show", "payment"):
                if not booking.show_id or booking.show.has_started:
                    skipped += 1
                    continue
                seat = Seat.objects.select_for_update().get(pk=booking.seat_id)
                Cancellation.objects.create(
                    user=booking.user,
                    show=booking.show,
                    payment=booking.payment,
                    seat_number=seat.seat_number,
                    refund_amount=booking.show.price,
                    reason="Cancelled by admin",
                )
                seat.is_booked = False
                seat.save(update_fields=["is_booked"])
                booking.delete()
                done += 1
        if done:
            self.message_user(request, f"{done} booking(s) cancelled; refunds are pending.", messages.SUCCESS)
        if skipped:
            self.message_user(
                request, f"{skipped} booking(s) skipped (show already started or no show linked).", messages.WARNING
            )


@admin.register(Cancellation)
class CancellationAdmin(admin.ModelAdmin):
    list_display = ["user", "seat_number", "show", "refund_amount", "refund_status", "cancelled_at", "refunded_at"]
    list_filter = ["refund_status"]
    search_fields = ["user__username", "payment__razorpay_payment_id"]
    actions = ["mark_refund_processed"]

    @admin.action(description="Mark selected pending refunds as processed")
    def mark_refund_processed(self, request, queryset):
        n = queryset.filter(refund_status=Cancellation.REFUND_PENDING).update(
            refund_status=Cancellation.REFUND_PROCESSED, refunded_at=timezone.now()
        )
        self.message_user(request, f"{n} refund(s) marked as processed.", messages.SUCCESS)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        "razorpay_order_id",
        "user",
        "show",
        "amount",
        "status",
        "razorpay_payment_id",
        "created_at",
    ]
    list_filter = ["status", "currency"]
    search_fields = ["razorpay_order_id", "razorpay_payment_id", "user__username"]
    readonly_fields = [
        "razorpay_order_id",
        "razorpay_payment_id",
        "razorpay_signature",
        "created_at",
        "updated_at",
    ]


@admin.register(SeatHold)
class SeatHoldAdmin(admin.ModelAdmin):
    """Mostly useful for debugging live reservations — holds normally expire and get
    cleaned up on their own within a couple of minutes."""

    list_display = ["seat", "show", "user", "created_at", "expires_at", "is_expired"]
    list_filter = ["show"]
    readonly_fields = ["created_at"]

    def is_expired(self, obj):
        return obj.is_expired()

    is_expired.boolean = True


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


@admin.register(MovieView)
class MovieViewAdmin(admin.ModelAdmin):
    """Recently viewed movies per user (drives "Recommended for You")."""

    list_display = ["user", "movie", "viewed_at"]
    search_fields = ["user__username", "movie__name"]
    readonly_fields = ["viewed_at"]
