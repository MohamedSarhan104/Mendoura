import datetime

from django.core.management.base import BaseCommand

from courses.models import LegalDocument, LegalSection

# English source of truth. Every other configured language (see
# settings.LANGUAGES) is populated by LegalDocument/LegalSection's own
# AI-translation pipeline (AutoTranslatedFieldsMixin, same as Track) --
# nothing here is ever hand-translated.
#
# Legal notice: working draft, not legal advice. Before publishing live this
# must be reviewed by a lawyer qualified in Egyptian e-commerce law, Egyptian
# Data Protection Law (Law No. 151 of 2020), and cross-border tax/payment
# matters, since the platform serves instructors and students of any
# nationality.
DOCUMENTS = {
    'terms': {
        'title': 'Terms & Conditions',
        'intro': (
            'Legal notice: This is a working draft, structured with reference to common '
            'practices on other educational platforms. It is not legal advice. Before '
            'publishing live, this must be reviewed by a lawyer qualified in Egyptian '
            'e-commerce law, Egyptian Data Protection Law (Law No. 151 of 2020), and '
            'cross-border tax/payment matters, since the platform serves instructors and '
            'students of all nationalities.'
        ),
        'sections': [
            ('introduction', '1. Introduction and Scope', """\
**Mendoura** ("the Platform," "we," "us") is an online learning platform that allows registered users to access recorded and live educational content (Students), and allows content creators (Instructors) to publish and sell courses through the Platform. The Platform is open to Students and Instructors of any nationality or country of residence, unless otherwise stated.

By using the Platform, whether as a Student or an Instructor, you agree to these Terms & Conditions in full. If you do not agree, you must stop using the Platform immediately.
"""),
            ('accounts', '2. Accounts', """\
- Users must be 18 years or older to independently create an account. Minors (under 18, but above the age of consent for online services in their country) may only use the Platform under a parent or legal guardian's supervision and consent.
- Users are solely responsible for the accuracy of the information they provide and for keeping their login credentials confidential.
- Accounts are personal and may not be shared or transferred to any other party. Sharing an account may result in permanent account closure without refund.
- The Platform reserves the right to suspend or terminate any account that violates these Terms without prior notice.
"""),
            ('student-accounts', '3. Student Accounts', """\
- A Student receives a limited, non-exclusive, non-transferable license to access content they have paid for (via a single-course purchase or a subscription), for personal, educational use only.
- Copying, redistributing, reselling, screen-recording, or sharing any content accessed through the Platform with any third party is prohibited.
- **Single-course purchase (Pay per course):** grants lifetime access to that specific course, for as long as the content remains available on the Platform for legal or policy reasons.
- **Subscription access:** grants access to all courses included in the subscription plan for the duration of the active subscription only. Access ends when the subscription expires or is cancelled — this is not a lifetime access license.
"""),
            ('instructor-accounts', '4. Instructor Accounts', """\
- Individuals of any nationality may register as an Instructor, subject to the identity verification criteria set by the Platform.
- Instructors retain full intellectual property ownership of the content they create, while granting the Platform a non-exclusive license to display, distribute, and promote that content through the Platform and its marketing channels.
- Instructors warrant that their content is original and does not infringe any third party's intellectual property rights, and bear full legal responsibility for any such infringement.
- The Platform reserves the right to review or remove any content that violates its policies or applicable law.
"""),
            ('revenue-share', '5. Instructor Revenue Share', """\
Instructor earnings from course sales are calculated as follows:

| Scenario | Instructor Share | Mendoura Share |
|---|---|---|
| **1. Student subscribes (Monthly or Yearly)** — revenue pooled from subscriptions is distributed across the courses consumed | **60%** | **40%** |
| **2. Student purchases a single course, and the Instructor edited/produced the video themselves** | **70%** | **30%** |
| **3. Student purchases a single course, and Mendoura produced/edited the video on the Instructor's behalf** | **50%** | **50%** |

**Notes on revenue distribution:**

- For subscriptions, each Instructor's share of pooled subscription revenue is calculated based on a fair usage/consumption methodology (e.g., minutes watched or lessons completed by subscribing students), detailed further in a separate Instructor Agreement.
- Instructor payouts are processed on a defined payment cycle (monthly), after deduction of any payment gateway processing fees applicable to the transaction.
- The Platform reserves the right to modify these revenue-share percentages in the future, provided Instructors are given reasonable advance notice, and no change applies retroactively to already-completed, paid transactions.
- In case of a dispute over who produced/edited a given course, the documented record between the Instructor and the Platform's team at the time of course submission shall govern.
"""),
            ('taxes', '6. Taxes (Instructor Responsibility)', """\
- **Mendoura is not responsible for any Instructor's personal or business tax obligations.** Each Instructor, regardless of nationality or country of residence, is solely and independently responsible for determining, declaring, and paying any income tax, VAT, withholding tax, or other applicable taxes arising from their earnings on the Platform, in accordance with the laws of their own country of tax residence.
- Mendoura's responsibility is limited to its own corporate tax obligations as a platform operating under Egyptian law, and does not extend to acting as a tax withholding agent or tax advisor for any Instructor, unless required to do so by binding Egyptian law.
- Instructors are encouraged to consult their own tax advisor regarding their specific obligations.
"""),
            ('payments', '7. Payments and Refunds', """\
- Displayed prices are final and include any applicable taxes under Egyptian law unless stated otherwise.
- **Platform subscriptions (Monthly/Yearly) are non-refundable** once payment is completed.
- **Individually purchased courses** are subject to a 14-day refund policy from the date of purchase, provided course-content consumption does not exceed a defined threshold (detailed in a separate Refund Policy).
- Subscriptions renew automatically according to the selected plan (Monthly/Yearly) until the user cancels auto-renewal before the next renewal date.
- **Payment processing:** For users and Instructors inside Egypt, payments are processed through **Paymob**. For international transactions, payments are processed through **Lemon Squeezy** as the payment gateway. The Platform does not store full card details; all payment processing is handled by these licensed third-party providers, subject to their own security and processing policies.
- **International Instructor payouts:** International Instructors (outside Egypt) must maintain a valid, verified **Payoneer** account in order to register as an Instructor and receive payouts. The Platform pays out an Instructor's share of earnings to their registered Payoneer account on the defined payment cycle. The Platform is not responsible for delays or issues caused by the Instructor's own Payoneer account (e.g., verification holds, incorrect account details).
"""),
            ('conduct', '8. Conduct Rules', """\
Users (Students and Instructors) may not:

- Post content that violates Egyptian or international law, incites hatred, or discriminates based on religion, ethnicity, gender, or disability.
- Impersonate others or provide misleading information.
- Attempt to hack, manipulate, or scrape the Platform's systems using automated tools or bots without authorization.
- Use the Platform for any unauthorized commercial purpose outside the agreed educational scope.
"""),
            ('liability', '9. Limitation of Liability', """\
The Platform's services are provided "as is" and "as available," without express or implied warranties regarding accuracy, continuity, or freedom from technical faults. The Platform is not liable for indirect or consequential damages arising from use of the Service, and its maximum liability in any case is limited to the amount the user paid during the 12 months preceding the claim.
"""),
            ('governing-law', '10. Governing Law', """\
These Terms are governed by and construed in accordance with the laws of the Arab Republic of Egypt, and the courts of Egypt shall have jurisdiction over any dispute arising from them, without prejudice to the rights of users outside Egypt under any mandatory consumer-protection law of their own jurisdiction that may apply.
"""),
            ('changes', '11. Changes to These Terms', """\
The Platform reserves the right to modify these Terms at any time, notifying users of material changes via registered email or an in-platform notice. Continued use of the Platform after such changes constitutes acceptance of them.
"""),
        ],
    },
    'privacy': {
        'title': 'Privacy Policy',
        'intro': '',
        'sections': [
            ('data-we-collect', '1. Data We Collect', """\
| Data Category | Description |
|---|---|
| **Account Data** | Name, email address, password (encrypted), phone number (optional) |
| **Profile Data** | Profile photo, bio (especially for Instructors), country, preferred language |
| **Learning Data** | Enrolled courses, completion progress, issued certificates, Q&A interactions with Instructors |
| **Payment Data** | Last 4 card digits, billing address (full card details are never stored; processed via a third-party payment gateway) |
| **Instructor Financial Data** | Bank account, or e-wallet details (Payoneer account for international Instructors) required to pay out Instructor earnings |
| **Technical Data** | IP address, device/browser type, cookies |
"""),
            ('legal-basis', '2. Legal Basis and Purpose of Processing', """\
We collect this data to:

- Provide the Service (account creation, granting course access, issuing certificates).
- Process payments and pay out Instructor earnings under the revenue-share model described above.
- Communicate with users about their account or Platform updates.
- Improve the Service and analyze usage for product development.
- Comply with legal or regulatory obligations applicable to the Platform.
"""),
            ('sharing-data', '3. Sharing Data with Third Parties', """\
We may share limited data with:

- **Payment gateway (Paymob)** to process payments.
- **Hosting and infrastructure providers** to operate the Platform and store files.
- **Payment processors** (Paymob for local transactions; Lemon Squeezy for international transactions; Payoneer for international Instructor payouts) to process payments and payouts.
- **Instructors**: limited data about a student (country, course progress) — never the student's personal email unless the student consents.
- **Government or judicial authorities**, when required by a valid legal request.

The Platform does not sell user data to any third party for marketing purposes without explicit consent.
"""),
            ('data-security', '4. Data Security', """\
Passwords are encrypted, and full card details are never stored on Platform servers. While we take commercially reasonable security measures, no internet-connected system can be guaranteed 100% secure.
"""),
            ('user-rights', '5. User Rights', """\
Users have the right to:

- Access their personal data and request a copy.
- Correct any inaccurate data.
- Request deletion of their account and data, subject to any legal obligation to retain certain records (e.g., financial transaction records).
- Withdraw consent to marketing communications at any time.

To exercise any of these rights, contact: support@mendoura.com.
"""),
            ('minors', '6. Minors Policy', """\
Users under 13 may not use the Platform under any circumstances. Users between 13 and 18 (or the age of consent for online services in their country) may only use the Platform with a parent or legal guardian's consent and supervision, and may not register as Instructors.
"""),
            ('cookies', '7. Cookies', """\
The Platform uses essential cookies required for the site to function, and functional cookies to improve the user experience (e.g., remembering language preference). Users can control cookies through their browser settings.
"""),
            ('changes', '8. Changes to This Policy', """\
The Platform reserves the right to update this Privacy Policy periodically, and will notify users of any material changes.
"""),
        ],
    },
}

LAST_UPDATED = datetime.date(2026, 7, 1)


class Command(BaseCommand):
    help = 'Sync the Terms & Conditions / Privacy Policy documents (idempotent, safe to re-run).'

    def handle(self, *args, **options):
        for slug, data in DOCUMENTS.items():
            document, created = LegalDocument.objects.update_or_create(
                slug=slug,
                defaults={
                    'title': data['title'],
                    'intro': data['intro'],
                    'last_updated': LAST_UPDATED,
                },
            )
            self.stdout.write(
                self.style.SUCCESS(f'{"Created" if created else "Synced"} legal document: {document.title}'))

            for order, (anchor, heading, body) in enumerate(data['sections'], start=1):
                section, section_created = LegalSection.objects.update_or_create(
                    document=document, anchor=anchor,
                    defaults={'order': order, 'heading': heading, 'body': body},
                )
                self.stdout.write(
                    f'  {"Created" if section_created else "Synced"}: {heading}')
