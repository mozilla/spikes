# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from os.path import basename
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
import six
import smtplib
from . import config


def send(To, Subject, Body,
         Cc=[], Bcc=[], html=False,
         files=[]):
    """Send an email
    """
    if isinstance(To, six.string_types):
        To = [To]

    From = config.get_sender()
    subtype = 'html' if html else 'plain'
    message = MIMEMultipart()
    message['From'] = From
    message['To'] = ', '.join(To)
    message['Subject'] = Subject
    message['Cc'] = ', '.join(Cc)
    message['Bcc'] = ', '.join(Bcc)

    message.attach(MIMEText(Body, subtype))

    for f in files:
        with open(f, "rb") as In:
            part = MIMEApplication(In.read(), Name=basename(f))
            f = basename(f)
            part['Content-Disposition'] = 'attachment; filename="%s"' % f
            message.attach(part)

    mailserver = smtplib.SMTP(config.get_smtp_server())
    mailserver.sendmail(From, To, message.as_string())
    mailserver.quit()
