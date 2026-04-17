from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User


class RegisterForm(UserCreationForm):
    email = forms.EmailField(
        required=True,
        label="Пошта",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "name@example.com"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].label = "Пароль"
        self.fields["password2"].label = "Підтвердіть пароль"
        self.fields["password1"].widget.attrs.update({"autocomplete": "new-password"})
        self.fields["password2"].widget.attrs.update({"autocomplete": "new-password"})

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise forms.ValidationError("Вкажіть пошту.")
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Користувач з такою поштою вже існує.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        email = (self.cleaned_data.get("email") or "").strip().lower()
        user.email = email
        # Keep username internal-only; equal to email to avoid exposing separate login handle.
        user.username = email[:150]
        if commit:
            user.save()
        return user

    class Meta:
        model = User
        fields = ("email", "password1", "password2")


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        label="Пошта",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "name@example.com"}),
    )


class ResendVerificationForm(forms.Form):
    email = forms.EmailField(
        required=True,
        label="Пошта",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "name@example.com"}),
    )
