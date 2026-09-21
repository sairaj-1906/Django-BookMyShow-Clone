from django import forms

from .models import Review, ReviewReport


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ["rating", "title", "body"]
        widgets = {
            "rating": forms.Select(
                choices=[(i, f"{i} star{'s' if i != 1 else ''}") for i in range(1, 6)],
                attrs={"class": "form-control"},
            ),
            "title": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Sum up your review (optional)",
                }
            ),
            "body": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "What did you think?",
                }
            ),
        }


class ReviewReportForm(forms.ModelForm):
    class Meta:
        model = ReviewReport
        fields = ["reason", "comment"]
        widgets = {
            "reason": forms.Select(attrs={"class": "form-control"}),
            "comment": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Optional details",
                }
            ),
        }
