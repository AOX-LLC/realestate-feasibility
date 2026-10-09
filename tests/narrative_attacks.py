"""Adversarial fixtures for the narrative figure checker.

The rule under test: a narrative may contain no number except an exact, standalone copy
of one of the allowed figure strings. The checker must also reject spelled-out quantities
and any character outside printable ASCII apart from a few typographic marks.

Fixture allowed figure strings:
    $420,000.00, $1,420,799.85, $1,313,239.71, $107,560.14, 7.57%, 33.69%, 44.92%,
    $324,271.50, -$95,728.50, 15.00%, 3,168 sq ft, 6,400 sq ft, 9 months, 7 comps,
    -$25,995.04, -$42,693.55, $84,670.08

ATTACKS: (id, text) pairs that the checker must REJECT.
DECOYS: (id, text) pairs that look suspicious but are allowed prose and must PASS.

Every non-ASCII or invisible character is written as a \\u escape so the source stays
plain ASCII and reviewable.
"""

ATTACKS: list[tuple[str, str]] = [
    # Rounded or truncated versions of allowed figures.
    ("rounded_no_cents", "Net profit lands near $107,560 after all costs are paid."),
    ("rounded_k_decimal", "We expect about $107.5k of profit on this flip."),
    ("rounded_pct_up", "The margin works out to about 7.6% on cost."),
    ("rounded_about_8", "That is about 8% on cost, which is thin for the risk."),
    ("truncated_cents", "The purchase price is $420,000 flat, per the contract."),
    # Magnitude suffixes.
    ("suffix_upper_k", "Buying at $420K looks fair against the comps."),
    ("suffix_m", "The after-repair value is $1.4M, according to the comps."),
    ("suffix_mm", "An exit near $1.4mm supports the plan."),
    ("suffix_grand", "Profit is 107 grand if everything goes well."),
    # Changed sign, or a figure glued to a sign.
    ("neg_pct", "The downside case shows -7.57% on cost."),
    ("plus_dollar", "Upside of +$324,271.50 is possible in a hot market."),
    ("unicode_minus_pct", "Margin shifts to \u22127.57% in the downside case."),
    ("unicode_minus_dollar", "Cash flow is \u2212$420,000.00 at closing."),
    ("sign_after_dollar", "The shortfall shows as $-95,728.50 in the model."),
    # Figure glued to letters or digits.
    ("glued_k_suffix", "Profit might reach $324,271.50k in a boom."),
    ("glued_letter_prefix", "A flip priced at x$420,000.00 is unusual."),
    ("glued_digit_prefix_pct", "Return on cost is 17.57% before holding costs."),
    ("glued_digit_prefix_dollar", "The cost basis 1$107,560.14 appears in the notes."),
    ("glued_digit_suffix", "Equity of $420,000.002 remains after closing."),
    # Figure inside a longer number.
    ("longer_prefix_digit", "A value of $11,420,799.85 was cited by the seller."),
    ("longer_suffix_digits", "Total cost reached $1,313,239.7100 with fees."),
    # Separator and decimal variants.
    ("european_separators", "The value of $1.420.799,85 was reported in the listing."),
    ("no_thousands_separator", "After-repair value is $1420799.85 in the model."),
    ("comma_decimal_pct", "A margin of 7,57% is thin for this market."),
    ("pct_spelled_percent", "The margin is 7.57 percent on cost."),
    # Spelled-out numbers in every style.
    ("spelled_hundred_seven_thousand", "Profit is one hundred and seven thousand dollars."),
    ("spelled_twenty_percent", "Expect twenty percent upside if the rehab stays on budget."),
    ("spelled_couple_hundred", "Overruns could reach a couple of hundred dollars per foot."),
    ("spelled_two_lots", "The seller also owns two lots next door."),
    ("spelled_three_quarters", "About three-quarters of the comps sold under list."),
    ("spelled_thirty_two", "Thirty-two days on market is typical here."),
    ("spelled_sixty_fold", "Repair costs rose sixty-fold once the old plumbing was found."),
    ("spelled_dozens", "Dozens of buyers toured similar homes this season."),
    ("spelled_millions", "Millions in upside are unlikely on a deal like this."),
    ("spelled_zero_margin", "Zero margin remains if the rehab slips."),
    # Ordinals, fractions and number-ish words.
    ("a_third_of", "A third of the comps are stale and should be ignored."),
    ("quarter_word", "Only a quarter of the budget covers the roof."),
    ("tenfold_word", "Risk grows tenfold if the permit is denied."),
    # Other digits.
    ("year_digits", "In 2026 the local market cooled noticeably."),
    ("zoning_code", "The lot is zoned R-7.5 residential."),
    ("lot_dimensions", "A 50x150 lot sits behind the main house."),
    ("phase_digit", "Phase 2 of the rehab covers the kitchen."),
    ("slash_digits", "The seller demanded 24/7 access during the rehab."),
    ("ordinal_3rd", "This is the 3rd comp that sold below list."),
    # Non-ASCII digits and look-alikes.
    ("arabic_indic_digits", "Expect \u0661\u0660\u0667 days of rehab before listing."),
    ("arabic_indic_in_figure", "A price of $\u0664\u0662\u0660,000.00 was quoted."),
    ("fullwidth_digits", "A margin of \uff17.\uff15\uff17% is thin."),
    ("fullwidth_percent_sign", "The margin is 7.57\uff05 on cost."),
    ("arabic_percent_sign", "The margin is 7.57\u066a on cost."),
    ("superscript_after_figure", "The lot of 6,400 sq ft\u00b2 is typical for the street."),
    ("circled_digit", "Option \u2460 is the best exit for this flip."),
    ("vulgar_fraction", "It sold for \u00bd the asking price."),
    ("roman_numeral_char", "Phase \u2163 of the rehab is the roof."),
    ("cjk_numeral", "The comp set has \u4e09 sales in the last quarter."),
    # Zero-width and invisible characters.
    ("zwsp_in_two", "There are tw\u200bo bids on the table."),
    ("zwnj_in_three", "Th\u200cree bidders showed up at the open house."),
    ("zwj_in_four", "Fo\u200dur offers arrived within a day."),
    ("word_joiner_in_fifteen", "Fif\u2060teen comps were pulled for the review."),
    ("bom_in_seventy", "Seven\ufeffty days remain until closing."),
    ("zwsp_inside_figure", "The price was $420,\u200b000.00 according to the file."),
    # Homoglyph letters inside number words.
    ("cyrillic_o_in_two", "There were tw\u043e bids on the property."),
    ("greek_omicron_in_four", "We counted f\u03bfur leaks in the basement."),
    ("cyrillic_e_in_three", "Inspectors found thr\u0435e cracks in the slab."),
    # Right-to-left override.
    ("rlo_reversed_amount", "Profit is \u202e00.701$ before costs."),
    ("rlo_around_allowed", "The margin of \u202e%75.7\u202c is 7.57% in the file."),
    # Numbers split across line breaks or words.
    ("digits_split_newline", "Rehab costs 1\n0\n7 dollars per foot."),
    ("digits_split_spaces", "The rehab runs 1 0 7 days."),
    # Allowed figures combined into arithmetic that implies a new number.
    ("arith_plus_new_figure", "The basis is $420,000.00 plus $5,000.00 in carrying costs."),
    ("arith_minus_percent", "Take $324,271.50 minus 10 percent for a safer estimate."),
    ("arith_times_factor", "Multiply $420,000.00 by 1.15 to get the ceiling."),
]

DECOYS: list[tuple[str, str]] = [
    ("decoy_price_and_arv", "The purchase price is $420,000.00, and the ARV is $1,420,799.85."),
    (
        "decoy_single_comp",
        "One single comp stands out, while the other 7 comps sit near the median.",
    ),
    ("decoy_half_of_hope", "Profit of $107,560.14 is about half of what the seller hoped for."),
    (
        "decoy_second_lot",
        "A second lot is not part of this deal, and the first lot is 6,400 sq ft.",
    ),
    ("decoy_double_home", "The house is 3,168 sq ft, roughly double a typical starter home."),
    ("decoy_twice_hold", "The hold time is 9 months, twice what the next flip needed."),
    ("decoy_margin_downside", "Margin on cost is 7.57%, and the downside case is -$25,995.04."),
    ("decoy_two_returns", "Return is 33.69% in the base case, and 44.92% is the best case."),
    ("decoy_all_in", "Costs are $1,313,239.71 all in, which is one number to watch."),
    ("decoy_loan_down", "The next step is a loan of $324,271.50 with 15.00% down."),
    ("decoy_shortfall", "The shortfall of -$95,728.50 is the first risk, a single line item."),
    ("decoy_rehab_half", "Rehab of $84,670.08 is half the gap, and -$42,693.55 is the other half."),
    ("decoy_twice_risk", "Twice the risk shows up when one bad comp meets 9 months on market."),
    ("decoy_price_lever", "At $420,000.00 the price is one lever, and 7 comps support it."),
    ("decoy_first_next", "The first scenario loses -$42,693.55, and the next recovers $84,670.08."),
]


# Added after the gatekeeper review: phrasings that passed the first spelled-number rule. All
# ASCII. Each must be rejected.
REVIEW_ATTACKS: list[tuple[str, str]] = [
    ("low_twenties", "The margin is in the low twenties."),
    ("low_teens", "The margin is in the low teens."),
    ("couple_of_comps", "A couple of comps are stale."),
    ("double_digit_loss", "Expect a double-digit loss."),
    ("hold_could_triple", "The hold could triple."),
    ("twelfth_comp", "This is the twelfth comp."),
    ("about_a_mil", "The ARV is about a mil."),
    ("beats_by_a_grand", "Profit beats the plan by a grand."),
    ("twentieth", "It is the twentieth lot on the block."),
    ("sixes", "Sixes and sevens everywhere."),
    ("zeroes", "Cut the zeroes."),
    ("twentyfive_joined", "About twentyfive percent."),
    ("fivehundred_joined", "A fivehundred buffer."),
    ("onehundred_joined", "An onehundred cushion."),
    ("camel_two_thousand", "TwoThousand dollars short."),
    ("underscore_two", "Plans for two_lots."),
    ("underscore_ten", "A _ten_ month hold."),
    ("fiftyish", "Fiftyish comps."),
    ("twoish", "Twoish weeks."),
    ("twentysomething", "A twentysomething margin."),
    ("a_score_of_comps", "A score of comps agree."),
    ("fourscore", "Fourscore comps agree."),
    ("gross_of_lots", "A gross of lots."),
    ("a_pair_of_lots", "A pair of lots."),
    ("a_trio", "A trio of risks."),
    ("treble_the_cost", "Treble the cost."),
    ("thrice", "Thrice the hold."),
    ("quadruple", "Quadruple the margin."),
    ("nil_margin", "Nil margin."),
    ("naught", "Naught left over."),
    ("single_digit", "A single-digit margin."),
    ("triple_digit", "A triple-digit return."),
    ("a_mil_and_a_half", "Half a mil of profit."),
    ("a_couple_mil", "A couple mil in value."),
    ("a_bil", "A bil in sales."),
    ("a_k", "Profit of a K."),
    ("a_g", "Profit of a G."),
    ("tenner", "A tenner short."),
    ("fiver", "A fiver more."),
    ("a_buck", "A buck over."),
    ("cnote", "A c-note over."),
    ("spanish_dos", "Dos lots on the site."),
    ("spanish_cien_mil", "Cien mil de profit."),
    ("german_zwei", "Zwei lots."),
    ("french_trois", "Trois comps."),
    ("roman_year", "Built MMXXVI."),
    ("roman_xx_percent", "A XX% margin."),
    ("roman_xii", "Lot XII on the plat."),
    ("roman_iv", "Phase IV of the plan."),
    ("lookalike_money", "Profit is $lOO,OOO.OO."),
    ("lookalike_percent", "A lO% margin."),
    ("lookalike_roman_dollar", "A $I,OOO fee."),
    ("lookalike_mixed", "A $lOl,OlO.lO cost."),
    ("lookalike_zero_percent", "A O.OO% margin."),
    ("lookalike_five_percent", "A lS% margin."),
    ("spaced_letters_two", "Plans for t w o lots."),
    ("hyphen_letters_two", "Plans for t-w-o lots."),
    ("dot_letters_two", "Plans for t.w.o lots."),
    ("spaced_capital_twenty", "A T W E N T Y month hold."),
    ("split_tw_o", "Plans for tw-o lots."),
    ("split_thou_sand", "About thou-sand dollars."),
    ("split_mil_lion", "About mil-lion dollars."),
    ("split_across_newline", "About thou\nsand dollars."),
    ("split_hun_dred", "About hun dred dollars."),
]

# Plain English that holds a number word inside it and must still pass.
REVIEW_DECOYS: list[tuple[str, str]] = [
    ("tenant", "The tenant has a lease and a tender offer."),
    ("often_content", "Often the content is the problem; attention to the lot helps."),
    ("canteen", "The canteen is closed."),
    ("fortune_comfort", "A fortune in comfort and effort."),
    ("weight_height", "Mind the weight and the height of the fence."),
    ("one_and_double", "One option is to double check; twice is better."),
    ("im_and_dont", "I'm sure they don't mind; it's fine."),
    ("tent_and_tense", "The tent is tense."),
]
