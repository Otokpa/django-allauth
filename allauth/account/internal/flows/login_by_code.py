from typing import Optional

from django.contrib import messages

from allauth.account import app_settings
from allauth.account.adapter import get_adapter
from allauth.account.internal.flows.code_verification import (
    AbstractCodeVerificationProcess,
)
from allauth.account.internal.flows.email_verification import (
    verify_email_indirectly,
)
from allauth.account.internal.flows.login import (
    perform_login,
    record_authentication,
)
from allauth.account.internal.flows.signup import send_unknown_account_mail
from allauth.account.internal.stagekit import clear_login, stash_login
from allauth.account.internal.userkit import user_id_to_str
from allauth.account.models import Login
from allauth.account.stages import LoginByCodeStage, LoginStageController


LOGIN_CODE_STATE_KEY = "login_code"


class LoginCodeVerificationProcess(AbstractCodeVerificationProcess):
    def __init__(self, stage):
        self.stage = stage
        self.request = stage.request
        super().__init__(
            state=stage.state,
            timeout=app_settings.LOGIN_BY_CODE_TIMEOUT,
            max_attempts=app_settings.LOGIN_BY_CODE_MAX_ATTEMPTS,
            user=stage.login.user,
        )

    def finish(self, redirect_url: Optional[str]):
        email = self.state["email"]
        user = self.user
        record_authentication(self.request, method="code", email=email)
        verify_email_indirectly(self.request, user, email)
        if self.state["initiated_by_user"]:
            # Just requesting a login code does is not considered to be a real login,
            # yet, is needed in order to make the stage machinery work. Now that we've
            # completed the code, let's start a real login.
            login = Login(
                user=user,
                redirect_url=redirect_url,
                email=email,
            )
            return perform_login(self.request, login)
        else:
            return self.stage.exit()

    def abort(self):
        clear_login(self.request)

    def persist(self):
        stash_login(self.request, self.stage.login)

    def send(self):
        email = self.state.get("email")
        phone = self.state.get("phone")
        if email:
            self.send_by_email(email)
        elif phone:
            self.send_by_phone(phone)
        else:
            raise ValueError()

    def send_by_phone(self, phone):
        # FIXME: logging or...
        if self.user:
            adapter = get_adapter()
            code = adapter.generate_phone_verification_code()
            adapter.send_phone_verification_code(
                user=self.user, phone=phone, code=code, signup=False
            )
            self.state["code"] = code

    def send_by_email(self, email):
        adapter = get_adapter()
        if not self.user:
            send_unknown_account_mail(self.request, email)
        else:
            code = adapter.generate_login_code()
            context = {
                "request": self.request,
                "code": code,
            }
            adapter.send_mail("account/email/login_code", email, context)
            self.state["code"] = code
        adapter.add_message(
            self.request,
            messages.SUCCESS,
            "account/messages/login_code_sent.txt",
            {"email": email},
        )

    @classmethod
    def initiate(
        cls, *, request, user, email: str = None, phone: str = None, stage=None
    ):
        initial_state = cls.initial_state(user=user, email=email, phone=phone)
        initial_state["initiated_by_user"] = stage is None
        if not stage:
            login = Login(user=user, email=email)
            login.state["stages"] = {"current": "login_by_code"}
            stage = LoginByCodeStage(
                LoginStageController(request, login), request, login
            )
        stage.state.update(initial_state)
        process = LoginCodeVerificationProcess(stage=stage)
        process.send()
        process.persist()
        return process

    @classmethod
    def resume(cls, stage):
        process = LoginCodeVerificationProcess(stage=stage)
        return process.abort_if_invalid()
