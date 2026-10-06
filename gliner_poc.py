"""
GLiNER proof-of-concept: test against known bskytest5 failures.
Compares current spaCy extraction vs GLiNER for the same posts.
"""
from gliner import GLiNER
import spacy

# ── Labels ──────────────────────────────────────────────────────────────────
LABELS = [
    "physical place a person is visiting or located at",
    "outdoor park, garden, or public space",
    "music venue, arena, amphitheater, or concert hall",
    "government facility, military base, or official building",
    "historical landmark, monument, or memorial",
    "neighborhood, district, or part of a city",
    "named geographic location or place",
]

# ── Test cases from bskytest5 ────────────────────────────────────────────────
# (post_text, expected_result, was_it_a_problem?)
TEST_CASES = [
    # --- Should extract (real locations) ---
    (
        "I saw him open for Bonnie Raitt at Red Rocks. He was badass!",
        "Red Rocks",
        "GEOCODED WRONG → Australia"
    ),
    (
        "Every year, Rob and I make sure to get our Summer Solstice picture at Gas Works. #gaypnw",
        "Gas Works",
        "GEOCODED WRONG → UK"
    ),
    (
        "Flu outbreak at Texas Air Force base nears 300 cases amid change to vaccine policy by Hegseth",
        "Texas Air Force base",
        "EXTRACTED WRONG → just 'Air Force' → Alaska"
    ),
    (
        "Remember the US government has made more arrests at the Reflecting Pool than the #DOJ",
        "the Reflecting Pool",
        "Correct in bskytest5 ✓"
    ),
    (
        "Try Allman Brothers Live at Fillmore East. As badass as it comes.",
        "Fillmore East",
        "GEOCODED WRONG → Minnesota"
    ),
    (
        "Mercedes impress in FP1 at the Red Bull Ring with Kimi Antonelli leading the way",
        "the Red Bull Ring",
        "GEOCODED WRONG → UK pub"
    ),
    (
        "There was an earthquake in El Centro CA this morning, only a 2.4",
        "El Centro",
        "Correct in bskytest5 ✓"
    ),
    # --- Should NOT extract (false positives) ---
    (
        "[🎬] Billboard Korea instagram update with ATEEZ\n\nWhat's your favorite track from GOLDEN HOUR : Part.5?",
        "NOTHING",
        "FALSE POSITIVE → geocoded to Idaho"
    ),
    (
        "BBC Radio 1\nSam and Danni\n\nNow Playing\nMK\nZone (feat. Poppy Baskcomb)",
        "NOTHING",
        "FALSE POSITIVE → geocoded to North Macedonia"
    ),
    (
        "The California Billionaire Tax Act would impose a tax on residents of the Golden State as of Jan. 1.",
        "NOTHING",
        "FALSE POSITIVE → geocoded to WA apartment"
    ),
    (
        "its still in my head, because its an Easter Egg in the Escape Velocity games.",
        "NOTHING",
        "FALSE POSITIVE → geocoded to New Hampshire"
    ),
    (
        "someone removed one at a time to a medley of Rod, Jane and Freddy songs",
        "NOTHING",
        "FALSE POSITIVE → geocoded to Rhode Island"
    ),
    (
        "this morning with some new music from Le Ren. The song is called Gold in California",
        "NOTHING",
        "FALSE POSITIVE → geocoded to Germany"
    ),
]

def run_spacy(texts, nlp):
    results = []
    for text in texts:
        doc = nlp(text)
        found = [ent.text for ent in doc.ents if ent.label_ in {"GPE", "LOC", "FAC"}]
        results.append(found)
    return results

def run_gliner(texts, model, threshold=0.4):
    results = []
    for text in texts:
        entities = model.predict_entities(text, LABELS, threshold=threshold)
        found = [e["text"] for e in entities]
        results.append(found)
    return results

def main():
    print("Loading models...")
    nlp = spacy.load("en_core_web_sm")
    gliner = GLiNER.from_pretrained("urchade/gliner_mediumv2.1")
    print("Models loaded.\n")

    texts = [t for t, _, _ in TEST_CASES]
    spacy_results = run_spacy(texts, nlp)
    gliner_results = run_gliner(texts, gliner)

    print("=" * 80)
    print(f"{'POST':<55} {'EXPECT':<25} {'spaCy':<30} {'GLiNER'}")
    print("=" * 80)

    spacy_correct = 0
    gliner_correct = 0

    for (text, expected, problem), spacy_out, gliner_out in zip(TEST_CASES, spacy_results, gliner_results):
        short_text = text.replace("\n", " ")[:52] + "..."
        spacy_str = str(spacy_out)[:28]
        gliner_str = str(gliner_out)[:28]

        should_find = expected != "NOTHING"
        spacy_ok = (should_find and any(expected.lower() in s.lower() for s in spacy_out)) or \
                   (not should_find and len(spacy_out) == 0)
        gliner_ok = (should_find and any(expected.lower() in s.lower() for s in gliner_out)) or \
                    (not should_find and len(gliner_out) == 0)

        spacy_mark = "✓" if spacy_ok else "✗"
        gliner_mark = "✓" if gliner_ok else "✗"
        if spacy_ok: spacy_correct += 1
        if gliner_ok: gliner_correct += 1

        print(f"\n  Post:    {short_text}")
        print(f"  Problem: {problem}")
        print(f"  Expect:  {expected}")
        print(f"  spaCy {spacy_mark}: {spacy_str}")
        print(f"  GLiNER {gliner_mark}: {gliner_str}")

    print("\n" + "=" * 80)
    print(f"SCORE  →  spaCy: {spacy_correct}/{len(TEST_CASES)}   GLiNER: {gliner_correct}/{len(TEST_CASES)}")
    print("=" * 80)

if __name__ == "__main__":
    main()
