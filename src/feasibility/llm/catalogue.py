"""The signal catalogue: the closed set of things the model may find in listing remarks.

This table is the one place a signal is defined. The extraction prompt is written from it, the
model's output schema takes its codes from it, and the eval's answer key is checked against it.
A change here changes the prompt and every recording made with it.

`include` and `exclude` are the guidance the prompt gives for the edges of each signal. They are
written from the meaning alone, never from eval cases. A negation, a hedge or a denial is never a
signal ("no HOA", "not in a flood zone", "foundation repaired, warranty"), whatever the code.
"""

from dataclasses import dataclass
from typing import Literal, get_args

Polarity = Literal["risk", "opportunity"]

# The codes are spelled out here so the type checker knows them; `CATALOGUE` below must list
# exactly these (a test asserts it).
SignalCode = Literal[
    "teardown_language",
    "as_is_sale",
    "environmental_hazard",
    "flood_or_drainage",
    "easement_or_encroachment",
    "deed_restrictions",
    "conservation_or_historic_district",
    "protected_trees",
    "tenant_occupied",
    "plans_or_permits",
    "seller_financing",
    "multiple_lots",
]


@dataclass(frozen=True)
class SignalDefinition:
    code: SignalCode
    polarity: Polarity
    meaning: str
    include: str
    exclude: str


CATALOGUE: tuple[SignalDefinition, ...] = (
    SignalDefinition(
        "teardown_language",
        "opportunity",
        "Remarks present the property as a teardown, lot value, or builder/investor special "
        "for new construction",
        "The house is described as having little or no value, the land is the asset, or the "
        "listing invites a builder or investor to demolish and build.",
        "A remodel, fixer or restoration pitch for keeping the house; a house sold in good "
        "condition that merely sits on a large lot.",
    ),
    SignalDefinition(
        "as_is_sale",
        "risk",
        "Sold as-is; seller makes no repairs",
        "The seller will sell as-is, make no repairs, or offer no warranty on condition.",
        "Repairs the seller already made; a home warranty offered; condition described "
        "without any statement that the seller will not repair.",
    ),
    SignalDefinition(
        "environmental_hazard",
        "risk",
        "Asbestos, underground tank, contamination, mold remediation: anything that raises "
        "demolition or site cost",
        "A hazard present on the property or in its buildings that would add cost to "
        "demolish or build: asbestos, an underground storage tank, contamination, mold that "
        "needs remediation.",
        "A hazard that was tested for and found absent, or a clean report; ordinary wear "
        "and cosmetic condition.",
    ),
    SignalDefinition(
        "flood_or_drainage",
        "risk",
        "Floodplain, flood zone, drainage or standing-water problems (not 'not in a flood zone')",
        "The property lies in a floodplain or flood zone, or has flooding, standing water or "
        "a drainage problem.",
        "A statement that the property is not in a flood zone or has no drainage issue; a "
        "creek or pond mentioned only as scenery.",
    ),
    SignalDefinition(
        "easement_or_encroachment",
        "risk",
        "Easements, encroachments, shared driveways, survey disputes",
        "An easement, an encroachment, a shared driveway or a boundary or survey dispute "
        "that affects the lot.",
        "A statement that there are no easements or encroachments; a survey that is merely "
        "available with no problem described.",
    ),
    SignalDefinition(
        "deed_restrictions",
        "risk",
        "Deed restrictions or HOA rules that limit what can be built",
        "Recorded covenants, deed restrictions or HOA rules that limit what may be built or how.",
        "A statement that there is no HOA or no restrictions; an HOA mentioned with no rule "
        "that limits building.",
    ),
    SignalDefinition(
        "conservation_or_historic_district",
        "risk",
        "Conservation district, historic overlay, demolition review or delay",
        "The property lies in a conservation district or historic overlay, or demolition "
        "needs a review, a commission approval or a waiting period.",
        "A statement that the property is outside any district; an old house described as "
        "charming with no district or review.",
    ),
    SignalDefinition(
        "protected_trees",
        "risk",
        "Large or protected trees, tree ordinance or mitigation",
        "Large or protected trees on the lot, a tree ordinance, or tree mitigation that "
        "constrains clearing or building.",
        "Trees mentioned only as scenery or shade; a statement that trees were removed or "
        "that there are no protected trees.",
    ),
    SignalDefinition(
        "tenant_occupied",
        "risk",
        "Occupied by a tenant; possession subject to a lease",
        "The property is occupied by a tenant, or possession is subject to a lease.",
        "A vacant property; an owner-occupied property; a former tenant who has left.",
    ),
    SignalDefinition(
        "plans_or_permits",
        "opportunity",
        "Plans, survey, engineering or permits available or approved",
        "Building plans, a survey, engineering or permits are available, included or approved.",
        "Plans or permits that are only possible or would have to be obtained by the buyer; "
        "a survey described as outdated or missing.",
    ),
    SignalDefinition(
        "seller_financing",
        "opportunity",
        "Seller will finance or carry a note",
        "The seller will finance the sale or carry a note.",
        "Cash-only or conventional terms; a buyer's lender or a hard-money lender mentioned "
        "as the buyer's own arrangement.",
    ),
    SignalDefinition(
        "multiple_lots",
        "opportunity",
        "Adjacent lot included or available, replat or split potential",
        "An adjacent lot is included or available, or the remarks describe replat or lot-split "
        "potential.",
        "A single large lot with no mention of a second lot, replat or split; a neighbour's "
        "lot mentioned only as a boundary.",
    ),
)

CODES: tuple[SignalCode, ...] = tuple(definition.code for definition in CATALOGUE)
DEFINITIONS: dict[str, SignalDefinition] = {definition.code: definition for definition in CATALOGUE}
LITERAL_CODES: tuple[str, ...] = get_args(SignalCode)
