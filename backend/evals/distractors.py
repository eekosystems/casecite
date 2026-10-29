"""Deterministic look-alike agreements that compete with the gold corpus.

A retrieval benchmark over eight documents measures nothing: every passage
is in the candidate pool. Real libraries hold dozens of leases with a late-fee
clause, dozens of loans that accelerate on default, dozens of NDAs with a
survival period. These generated agreements reproduce that competition with
different parties and numbers, so a question about the Calloway lease's late
charge has to find *that* lease's clause among many similar ones.

The text is generated from a fixed seed, so the corpus (and every metric run
against it) is reproducible. None of the gold parties, defined terms or
product codes appear here.
"""

from __future__ import annotations

import random
from collections.abc import Callable

COMPANIES = [
    "Alder Creek Logistics, Inc.",
    "Bluewater Dental Group, P.C.",
    "Cobalt Bay Software, Inc.",
    "Dunmore Freight Lines, LLC",
    "Elmhurst Pediatrics, P.A.",
    "Foxglove Media Group, Inc.",
    "Granite Peak Builders, LLC",
    "Hollis & Vane Architects, LLP",
    "Ironwood Hospitality Group, Inc.",
    "Juniper Labs, Inc.",
    "Kingsbury Textiles Corp.",
    "Lakeshore Orthopedics, P.C.",
    "Marlow Robotics, Inc.",
    "Nettle Grove Farms, LLC",
    "Oakhaven Senior Living, Inc.",
    "Pinecrest Data Systems, Inc.",
    "Quarry Hill Brewing Co.",
    "Redfern Clinical Research, LLC",
    "Saltmarsh Seafood Co.",
    "Tamarack Energy Partners, L.P.",
    "Umber Studios, LLC",
    "Vantage Point Imaging, Inc.",
    "Westbrook Pharmacy Services, Inc.",
    "Yarrow Biosciences, Inc.",
    "Zephyr Aerial Imaging, LLC",
    "Ashgrove Veterinary Partners, LLC",
    "Brambleton Packaging Corp.",
    "Cinderhill Foods, Inc.",
    "Driftwood Marine Supply, Inc.",
    "Evergold Solar, LLC",
]
LANDLORDS = [
    "Maple Court Realty LLC",
    "Seaview Holdings LLC",
    "Brookfield Plaza Associates",
    "Copper Street Partners LLC",
    "Lantern Square Owner LLC",
    "Millrace Commons LLC",
]
LENDERS = [
    "Summit Crest Bank, N.A.",
    "Fairhaven Credit Partners, L.P.",
    "Blue Heron Capital, LLC",
    "Northgate Commercial Finance, Inc.",
]
PEOPLE = [
    "Jordan Ellis",
    "Samira Haddad",
    "Kenji Watanabe",
    "Lucia Moreno",
    "Owen Fitzgerald",
    "Grace Adeyemi",
    "Tobias Lindqvist",
    "Hannah Brooks",
    "Mateo Alvarez",
    "Nadia Petrova",
]
STATES = [
    "Delaware",
    "New York",
    "California",
    "Texas",
    "Illinois",
    "Georgia",
    "Washington",
    "Colorado",
    "Ohio",
    "Florida",
]
CITIES = ["Albany", "Tacoma", "Savannah", "Boulder", "Dayton", "Tampa", "Austin", "Fresno"]

_WORDS = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    10: "ten",
    12: "twelve",
    14: "fourteen",
    15: "fifteen",
    18: "eighteen",
    20: "twenty",
    24: "twenty-four",
    30: "thirty",
    36: "thirty-six",
    45: "forty-five",
    48: "forty-eight",
    60: "sixty",
    90: "ninety",
    120: "one hundred twenty",
    150: "one hundred fifty",
    270: "two hundred seventy",
}


def _n(value: int) -> str:
    """'thirty (30)' style, as agreements write numbers."""
    return f"{_WORDS[value]} ({value})" if value in _WORDS else str(value)


def _money(value: int) -> str:
    return f"${value:,.2f}"


def _date(rng: random.Random) -> str:
    month = rng.choice(
        ["January", "March", "April", "June", "August", "October", "November", "December"]
    )
    return f"{month} {rng.randint(1, 28)}, {rng.choice([2022, 2023, 2024, 2025])}"


def _two_companies(rng: random.Random) -> tuple[str, str]:
    a, b = rng.sample(COMPANIES, 2)
    return a, b


def services_agreement(rng: random.Random) -> str:
    provider, customer = _two_companies(rng)
    pay = rng.choice([15, 30, 45, 60])
    cure = rng.choice([10, 15, 30, 60])
    cap_months = rng.choice([6, 12, 24])
    renew = rng.choice([30, 60, 90])
    uptime = rng.choice(["99.0%", "99.5%", "99.8%", "99.95%"])
    state = rng.choice(STATES)
    return f"""PROFESSIONAL SERVICES AGREEMENT

This Professional Services Agreement is entered into as of {_date(rng)} between {provider} ("Provider") and {customer} ("Client").

1. SERVICES
1.1 Provider shall perform the services described in each statement of work in a professional manner consistent with industry standards.
1.2 Client shall provide timely access to personnel, systems and information reasonably required for the services.

2. FEES AND PAYMENT
2.1 Client shall pay the fees in each statement of work. Provider shall invoice monthly in arrears.
2.2 Client shall pay each invoice within {_n(pay)} days after receipt. Late amounts bear interest at one and one-half percent (1.5%) per month.
2.3 Client may withhold disputed amounts in good faith by giving written notice describing the dispute before the payment due date.

3. SERVICE LEVELS
3.1 Provider shall make the hosted components of the services available {uptime} of the time in each month, excluding scheduled maintenance.
3.2 If availability falls below that level, Client is entitled to service credits as set out in the applicable statement of work, which are Client's exclusive remedy for unavailability.

4. TERM AND TERMINATION
4.1 This Agreement has an initial term of {_n(rng.choice([12, 24, 36]))} months and renews for successive one-year terms unless either party gives notice of non-renewal at least {_n(renew)} days before the end of the term.
4.2 Either party may terminate this Agreement if the other party materially breaches it and fails to cure the breach within {_n(cure)} days after written notice describing the breach.
4.3 Either party may terminate immediately if the other party becomes insolvent or makes an assignment for the benefit of creditors.
4.4 Upon a change of control of Client, Provider may terminate this Agreement on {_n(60)} days' notice; no fees are accelerated by a change of control.

5. CONFIDENTIALITY
5.1 Each party shall hold the other party's confidential information in confidence and use it only to perform this Agreement, for {_n(rng.choice([2, 3, 5]))} years after termination.

6. INDEMNIFICATION
6.1 Provider shall indemnify Client against third-party claims that the deliverables infringe intellectual property rights.
6.2 Client shall indemnify Provider against third-party claims arising from materials Client provides.

7. LIMITATION OF LIABILITY
7.1 Neither party is liable for consequential, incidental or punitive damages.
7.2 Each party's total liability shall not exceed the fees paid in the {_n(cap_months)} months before the claim, except for breaches of confidentiality and indemnification obligations.

8. GENERAL
8.1 This Agreement is governed by the laws of the State of {state}.
8.2 Neither party may assign this Agreement without the other's consent, except to a successor in a merger or sale of substantially all assets.
8.3 Notices must be in writing and delivered by hand, courier or email to the addresses on the signature page.
"""


def commercial_lease(rng: random.Random) -> str:
    landlord = rng.choice(LANDLORDS)
    tenant = rng.choice(COMPANIES)
    late_pct = rng.choice([3, 4, 6, 8, 10])
    grace = rng.choice([3, 5, 7, 15])
    deposit = rng.choice([9_000, 12_500, 22_000, 30_000])
    share = rng.choice(["5.2%", "6.75%", "11.1%", "12.5%", "3.9%"])
    rent = rng.choice([48_000, 60_000, 96_000, 120_000])
    notice_min = rng.choice([90, 120, 270])
    city = rng.choice(CITIES)
    state = rng.choice(STATES)
    return f"""RETAIL LEASE

This Retail Lease is made as of {_date(rng)} between {landlord} ("Landlord") and {tenant} ("Tenant").

ARTICLE 1. PREMISES AND TERM
1.1 Landlord leases to Tenant the premises at {rng.randint(100, 9999)} Main Street, {city}, {state}, containing approximately {rng.randint(1200, 6000):,} rentable square feet.
1.2 The term is {_n(rng.choice([3, 5, 7, 10]))} years beginning on the commencement date.

ARTICLE 2. RENT
2.1 Tenant shall pay annual base rent of {_money(rent)} in equal monthly installments due on the first day of each month, increasing by {_n(3)} percent each lease year.
2.2 If rent is not paid within {_n(grace)} days after it is due, Tenant shall pay a late fee of {late_pct} percent of the unpaid amount, plus interest at the default rate.
2.3 Tenant shall deposit {_money(deposit)} as a security deposit, to be returned within {_n(30)} days after the lease ends, less amounts applied to Tenant's obligations.

ARTICLE 3. OPERATING COSTS
3.1 Tenant shall pay its pro rata share, being {share}, of common area maintenance charges, real estate taxes and insurance for the shopping center.
3.2 Landlord shall provide an annual statement reconciling estimated and actual operating costs.

ARTICLE 4. USE
4.1 Tenant may use the premises only for the permitted use stated in the lease summary and for no other purpose.
4.2 Landlord grants no exclusive use rights to Tenant.

ARTICLE 5. ASSIGNMENT
5.1 Tenant may not assign this lease or sublet the premises without Landlord's written consent, which may not be unreasonably withheld.
5.2 A transfer of more than fifty percent of the ownership interests in Tenant is an assignment requiring consent.

ARTICLE 6. CASUALTY AND CONDEMNATION
6.1 If the premises are damaged by fire or other casualty and cannot be restored within {_n(rng.choice([90, 120, 150]))} days, either party may terminate this lease.
6.2 Rent abates during restoration in proportion to the unusable area.

ARTICLE 7. RENEWAL
7.1 Tenant has the option to renew for one additional term of {_n(5)} years by giving notice at least {_n(notice_min)} days before the lease expires, at a rent equal to the prevailing market rent.

ARTICLE 8. DEFAULT
8.1 Tenant is in default if it fails to pay rent within {_n(grace)} days after notice or fails to cure any other breach within {_n(30)} days after notice.
8.2 Upon default Landlord may terminate the lease, recover possession and pursue damages.

ARTICLE 9. MISCELLANEOUS
9.1 This lease is governed by the laws of the State of {state}.
9.2 Tenant shall maintain commercial general liability insurance of at least {_money(rng.choice([1_000_000, 3_000_000]))} per occurrence.
9.3 Tenant has a right of first offer on adjacent space that becomes available during the term.
"""


def loan_agreement(rng: random.Random) -> str:
    lender = rng.choice(LENDERS)
    borrower = rng.choice(COMPANIES)
    principal = rng.choice([1_500_000, 2_750_000, 6_000_000, 10_000_000])
    margin = rng.choice(["2.50%", "2.75%", "3.50%", "4.00%"])
    fccr = rng.choice(["1.10", "1.15", "1.25", "1.35"])
    leverage = rng.choice(["2.50", "3.50", "4.00"])
    cross = rng.choice([100_000, 500_000, 1_000_000])
    grace = rng.choice([3, 10, 15])
    state = rng.choice(STATES)
    return f"""CREDIT AGREEMENT

This Credit Agreement is dated as of {_date(rng)} between {lender} ("Lender") and {borrower} ("Borrower").

SECTION 1. THE FACILITY
1.1 Lender agrees to make a term loan to Borrower in the principal amount of {_money(principal)}.
1.2 The loan bears interest at Term SOFR plus {margin} per annum, payable monthly.
1.3 Borrower may prepay the loan at any time without premium or penalty, together with accrued interest.

SECTION 2. COVENANTS
2.1 Borrower shall maintain a fixed charge coverage ratio of at least {fccr} to 1.00, measured quarterly on a trailing twelve month basis.
2.2 Borrower shall maintain a total leverage ratio of no more than {leverage} to 1.00.
2.3 Borrower shall deliver monthly borrowing base certificates and quarterly financial statements.

SECTION 3. EVENTS OF DEFAULT
3.1 Each of the following is an event of default: (a) failure to pay principal or interest within {_n(grace)} days after the due date; (b) breach of any financial covenant; (c) a default under other debt exceeding {_money(cross)}; (d) a change of control of Borrower; (e) bankruptcy or insolvency of Borrower.
3.2 Upon an event of default, Lender may terminate its commitment and declare the loan immediately due and payable. Upon a bankruptcy event of default the loan is due automatically.
3.3 After an event of default, interest accrues at the default rate of {_n(rng.choice([2, 3, 4]))} percent above the otherwise applicable rate.

SECTION 4. SECURITY
4.1 The obligations are secured by a first priority lien on all assets of Borrower under the security agreement.

SECTION 5. MISCELLANEOUS
5.1 This Agreement is governed by the laws of the State of {state}.
5.2 Borrower shall reimburse Lender's reasonable costs of enforcement, including attorneys' fees.
"""


def supply_agreement(rng: random.Random) -> str:
    supplier, buyer = _two_companies(rng)
    volume = rng.choice([50_000, 150_000, 250_000, 750_000])
    shortfall = rng.choice(["$0.10", "$0.20", "$0.50", "$0.75"])
    cap = rng.choice([2, 3, 5, 6])
    pay = rng.choice([30, 60, 90])
    fm_days = rng.choice([30, 60, 120])
    state = rng.choice(STATES)
    return f"""MANUFACTURING AND SUPPLY AGREEMENT

This Manufacturing and Supply Agreement is entered into as of {_date(rng)} between {supplier} ("Seller") and {buyer} ("Purchaser").

1. SUPPLY
1.1 Seller shall manufacture and supply the products listed in the pricing exhibit in accordance with purchase orders submitted by Purchaser.
1.2 Purchaser shall purchase at least {volume:,} units of products in each contract year. If Purchaser fails to do so, Purchaser shall pay {shortfall} for each unit of shortfall.

2. PRICE
2.1 Prices are fixed for the first contract year. Thereafter Seller may increase prices once per year on {_n(60)} days' notice by no more than {cap} percent.
2.2 Purchaser shall pay invoices within {_n(pay)} days after receipt.

3. DELIVERY AND RISK
3.1 Products are delivered DAP Purchaser's facility (Incoterms 2020); risk of loss passes on delivery at that facility.

4. WARRANTY
4.1 Seller warrants that products conform to specifications for {_n(rng.choice([6, 18, 24]))} months after delivery and will repair or replace non-conforming products.

5. FORCE MAJEURE
5.1 A party is excused from performance to the extent prevented by causes beyond its reasonable control, including payment obligations where banking systems are unavailable.
5.2 If a force majeure event continues for more than {_n(fm_days)} days, either party may terminate this Agreement on written notice.

6. GENERAL
6.1 This Agreement is governed by the laws of the State of {state}.
6.2 The term of this Agreement is {_n(rng.choice([2, 3, 5]))} years.
"""


def employment_agreement(rng: random.Random) -> str:
    employer = rng.choice(COMPANIES)
    person = rng.choice(PEOPLE)
    title = rng.choice(
        [
            "Chief Financial Officer",
            "Vice President of Sales",
            "Director of Operations",
            "Chief Marketing Officer",
            "Vice President of Product",
        ]
    )
    salary = rng.choice([180_000, 210_000, 240_000, 310_000])
    bonus = rng.choice([15, 20, 25, 40])
    months = rng.choice([3, 4, 9, 12])
    nc = rng.choice([6, 18, 24])
    miles = rng.choice([25, 50, 75])
    state = rng.choice(STATES)
    return f"""EMPLOYMENT AGREEMENT

This Employment Agreement is made as of {_date(rng)} between {employer} (the "Employer") and {person} (the "Employee").

1. POSITION
1.1 The Employer employs the Employee as {title}.

2. COMPENSATION
2.1 The Employer shall pay the Employee an annual base salary of {_money(salary)}.
2.2 The Employee is eligible for an annual bonus targeted at {bonus} percent of base salary.

3. TERMINATION
3.1 Either party may end the employment on {_n(30)} days' written notice.
3.2 If the Employer terminates the employment without cause, the Employer shall pay severance equal to {_n(months)} months of base salary, subject to the Employee signing a release of claims.
3.3 "Good reason" means a material reduction in base salary or a relocation of more than {_n(miles)} miles, in each case after notice and an opportunity to cure.

4. RESTRICTIVE COVENANTS
4.1 For {_n(nc)} months after employment ends, the Employee shall not work for a competitor within {_n(miles)} miles of any Employer office.
4.2 For {_n(12)} months after employment ends, the Employee shall not solicit Employer's employees or customers.

5. GENERAL
5.1 This Agreement is governed by the laws of the State of {state}.
"""


def nda(rng: random.Random) -> str:
    a, b = _two_companies(rng)
    period = rng.choice([1, 2, 3, 5])
    survive = rng.choice([2, 4, 5, 7])
    ret = rng.choice([10, 20, 30])
    state = rng.choice(STATES)
    return f"""CONFIDENTIALITY AGREEMENT

This Confidentiality Agreement is entered into as of {_date(rng)} between {a} and {b} (each, a "Party").

1. CONFIDENTIAL INFORMATION
1.1 "Confidential Information" means non-public information disclosed by one Party to the other in connection with evaluating a potential business relationship.
1.2 Confidential Information excludes information that is public, already known to the recipient, received from a third party without restriction, or independently developed.

2. OBLIGATIONS
2.1 The recipient shall keep Confidential Information confidential and use it only to evaluate the potential relationship.
2.2 If disclosure is required by law, the recipient shall notify the discloser promptly and cooperate in seeking a protective order.

3. TERM
3.1 This Agreement covers disclosures made during the {_n(period)} year period after its date, and the obligations of confidentiality survive for {_n(survive)} years after that period ends.
3.2 No residuals clause applies; information retained in memory remains Confidential Information.

4. RETURN OF MATERIALS
4.1 Upon request the recipient shall return or destroy all Confidential Information within {_n(ret)} days.

5. GENERAL
5.1 This Agreement is governed by the laws of the State of {state}, and the courts of that state have exclusive jurisdiction.
"""


def asset_purchase(rng: random.Random) -> str:
    buyer, seller = _two_companies(rng)
    price = rng.choice([3_000_000, 8_500_000, 25_000_000])
    basket = rng.choice(["0.5%", "0.75%", "2%"])
    cap = rng.choice(["10%", "20%", "25%"])
    survival = rng.choice([12, 24, 36])
    state = rng.choice(STATES)
    return f"""ASSET PURCHASE AGREEMENT

This Asset Purchase Agreement is made as of {_date(rng)} between {buyer} ("Purchaser") and {seller} ("Vendor").

ARTICLE 1. PURCHASE
1.1 Vendor shall sell, and Purchaser shall buy, the assets of Vendor's business listed in the disclosure schedule for a purchase price of {_money(price)}.
1.2 Ten percent of the purchase price shall be held in escrow for {_n(survival)} months to secure Vendor's indemnification obligations.

ARTICLE 2. INDEMNIFICATION
2.1 Vendor's representations survive for {_n(survival)} months after closing.
2.2 Vendor shall indemnify Purchaser for losses from breaches of representations once losses exceed a tipping basket of {basket} of the purchase price, after which Vendor is liable from the first dollar.
2.3 Vendor's indemnification liability is capped at {cap} of the purchase price, except for fraud.
2.4 Purchaser may not recover for any breach of which it had knowledge before closing.

ARTICLE 3. GENERAL
3.1 This Agreement is governed by the laws of the State of {state}.
"""


GENERATORS: list[Callable[[random.Random], str]] = [
    services_agreement,
    commercial_lease,
    loan_agreement,
    supply_agreement,
    employment_agreement,
    nda,
    asset_purchase,
]


def generate(count: int = 42, seed: int = 20261028) -> list[tuple[str, str]]:
    """``count`` (filename, text) look-alike agreements, cycling through the types."""
    rng = random.Random(seed)
    docs: list[tuple[str, str]] = []
    for i in range(count):
        gen = GENERATORS[i % len(GENERATORS)]
        docs.append((f"distractor_{i:03d}_{gen.__name__}.txt", gen(rng)))
    return docs
