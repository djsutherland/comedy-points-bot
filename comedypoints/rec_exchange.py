from logging import getLogger
import os

from discord.ext import commands

logger = getLogger(__name__)


class RecExchange(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def service_account(self):
        if not hasattr(self, "_gc"):
            self._gc = gspread.service_account_from_dict(
                {
                    "type": "service_account",
                    "project_id": os.environ["GOOGLE_PROJECT_ID"],
                    "private_key_id": os.environ["GOOGLE_PRIVATE_KEY_ID"],
                    "private_key": os.environ["GOOGLE_PRIVATE_KEY"],
                    "client_email": os.environ["GOOGLE_CLIENT_EMAIL"],
                    "client_id": os.environ["GOOGLE_CLIENT_ID"],
                    "client_x509_cert_url": os.environ["GOOGLE_CLIENT_X509_CERT_URL"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "universe_domain": "googleapis.com",
                }
            )
        return self._gc

    def sheet(self):
        if not hasattr(self, "_sheet"):
            self._sheet = self.service_account().open_by_key(
                os.environ["REC_EXCHANGE_SPREADSHEET_KEY"]
            )
        return self._sheet

    def participants_map(self):
        if not hasattr(self, '_participants'):
            pts = self.sheet().get_worksheet_by_id(
                os.environ["REC_EXCHANGE_PARTICIPANTS_ID"]
            )
            vals = pts.get_all_values()

            #channel = self.bot.get_channel(os.environ["REC_EXCHANGE_CHANNEL"])
            self._participants = {}
            for name, uid in vals[1:]:
                #user = self.bot.get_user(uid)
                self._participants[int(uid)] = name
        return self._participants


async def setup(bot):
    await bot.add_cog(RecExchange(bot))
