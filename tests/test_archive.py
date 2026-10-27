"""Moving applied-to jobs, their CVs and their postings into data/archive/."""
from datetime import date

import pytest

from jobagent import archive, config, postings, store
from jobagent.cv.tailor import out_dir_for


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "JOBS_CSV", tmp_path / "jobs.csv")
    monkeypatch.setattr(config, "SEEN_CSV", tmp_path / "seen.csv")
    return tmp_path


def track(n, company="Acme", tailored=True, letter=False, **fields):
    url = f"https://jobs.lever.co/{company.lower()}/{n}"
    job = {"company": company, "role": f"Engineer {n}", "url": url, "market": "Singapore",
           "location": "Singapore", "why": "fits"}
    store.append_jobs([job])
    postings.save(url=url, company=company, role=job["role"], text=f"Posting {n} text.")
    if tailored:
        folder = out_dir_for(job)
        folder.mkdir(parents=True)
        (folder / "Jane_Example_CV.pdf").write_bytes(b"%PDF cv")
        (folder / "review.json").write_text("{}")
        fields["cv"] = str((folder / "Jane_Example_CV.pdf").relative_to(config.DATA_DIR))
        if letter:
            (folder / "Jane_Example_Cover_Letter.pdf").write_bytes(b"%PDF letter")
            fields["cover_letter"] = str((folder / "Jane_Example_Cover_Letter.pdf").relative_to(config.DATA_DIR))
    if fields:
        store.update_job(url, **fields)
    return url


def test_applied_jobs_move_to_the_archive_with_cv_letter_and_posting(data):
    applied = track(1, letter=True, applied="Yes")
    untailored = track(2, tailored=False, applied="x")
    waiting = track(3)
    archived = archive.archive_applied(today=date(2026, 10, 23))

    assert sorted(r["url"] for r in archived) == sorted([applied, untailored])
    assert [r["url"] for r in store.read_jobs()] == [waiting]

    rows = {r["url"]: r for r in store.read_archived()}
    row = rows[applied]
    assert row["archived"] == "2026-10-23" and row["folder"].startswith("archive/")
    assert row["cv"] == row["folder"] + "/Jane_Example_CV.pdf"
    assert row["cover_letter"] == row["folder"] + "/Jane_Example_Cover_Letter.pdf"
    folder = data / row["folder"]
    assert (data / row["cv"]).read_bytes() == b"%PDF cv" and (data / row["cover_letter"]).exists()
    assert (folder / "review.json").exists() and "Posting 1 text." in (folder / "posting.md").read_text()
    assert not (data / "applications" / folder.name).exists()
    # A job never tailored still gets its folder, with the posting.
    assert "Posting 2 text." in (data / rows[untailored]["folder"] / "posting.md").read_text()
    # The waiting job's own folder is untouched.
    assert (data / store.read_jobs()[0]["cv"]).exists()


def test_archived_jobs_are_never_recorded_again(data):
    url = track(1, applied="yes")
    archive.archive_applied()
    assert store.job_key(url) in store.known_keys()  # kept in seen.csv
    again = {"company": "Acme", "role": "Engineer 1", "url": url, "market": "Singapore", "why": "x"}
    repost = dict(again, url="https://acme.example/careers/engineer-1")
    res = store.append_jobs([again, repost])
    assert res["added"] == 0 and {r["reason"] for r in res["rejected"]} == {"already tracked"}


def test_nothing_marked_applied_changes_nothing(data):
    track(1)
    before = config.JOBS_CSV.read_text()
    assert archive.archive_applied() == [] and config.JOBS_CSV.read_text() == before
    assert not (data / "archive").exists()


def test_your_own_columns_survive_on_both_sides(data):
    url = track(1, applied="done")
    keep = track(2)
    text = config.JOBS_CSV.read_text().splitlines()
    rows = [text[0] + ",my_notes"] + [line + (",called recruiter" if "/1," in line else ",") for line in text[1:]]
    config.JOBS_CSV.write_text("\n".join(rows) + "\n")
    archive.archive_applied()
    assert store.read_archived()[0]["my_notes"] == "called recruiter"
    assert list(store.read_jobs()[0]) == store.COLUMNS + ["my_notes"] and store.read_jobs()[0]["url"] == keep
    assert url not in config.JOBS_CSV.read_text()


def test_a_cv_that_finishes_after_archiving_joins_its_folder(data):
    url = track(1, tailored=False, applied="yes")
    archive.archive_applied()
    row = store.read_archived()[0]
    late = data / "applications" / out_dir_for(row).name
    late.mkdir(parents=True)
    (late / "Jane_Example_CV.pdf").write_bytes(b"late cv")
    archive.archive_applied()  # the next run
    assert (data / row["folder"] / "Jane_Example_CV.pdf").read_bytes() == b"late cv"
    assert not late.exists() and store.read_archived()[0]["url"] == url


def test_a_taken_archive_folder_name_gets_a_suffix(data):
    url = track(1, applied="yes")
    row = next(r for r in store.read_jobs() if r["url"] == url)
    (data / "archive" / (data / row["cv"]).parent.name).mkdir(parents=True)
    archive.archive_applied()
    assert store.read_archived()[0]["folder"].endswith("-2")


def test_jobs_marked_no_leave_the_tracker_and_their_cvs_are_deleted(data):
    declined = track(1, letter=True, applied="No")
    untailored = track(2, tailored=False, applied="skip")
    waiting = track(3)
    applied = track(4, applied="yes")
    folder = data / store.read_jobs()[0]["cv"]
    archived, removed = archive.archive_marked(today=date(2026, 10, 27))

    assert [r["url"] for r in archived] == [applied]
    assert sorted(r["url"] for r in removed) == sorted([declined, untailored])
    assert [r["url"] for r in store.read_jobs()] == [waiting]
    rows = {r["url"]: r for r in store.read_declined()}
    assert set(rows) == {declined, untailored} and rows[declined]["archived"] == "2026-10-27"
    assert rows[declined]["cv"] == "" and rows[declined]["cover_letter"] == ""
    assert not folder.parent.exists() and postings.load(declined) is None
    assert [r["url"] for r in store.read_archived()] == [applied]  # applied.csv holds only applied
    # Still counts as tracked: never recorded again, nor a repost under a new URL.
    again = store.append_jobs([{"company": "Acme", "role": "Engineer 1", "url": declined,
                                "market": "Singapore", "location": "Singapore", "why": "x"}])
    repost = store.append_jobs([{"company": "Acme", "role": "Engineer 1", "market": "Singapore",
                                 "url": "https://jobs.lever.co/acme/999", "location": "Singapore",
                                 "why": "x"}])
    assert not again.get("added") and not repost.get("added")
    # A second run finds nothing more to do.
    assert archive.archive_marked() == ([], [])


def test_a_cv_finishing_after_its_job_was_turned_down_is_deleted(data):
    url = track(1, tailored=False, applied="no")
    archive.archive_marked()
    late = data / "applications" / out_dir_for(store.read_declined()[0]).name
    late.mkdir(parents=True)
    (late / "Jane_Example_CV.pdf").write_bytes(b"%PDF late")
    archive.archive_marked()
    assert not late.exists()
