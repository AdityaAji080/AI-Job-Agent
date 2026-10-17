"""Trimmed API responses in each system's documented shape."""

GREENHOUSE = {"jobs": [
    {"id": 8651349002, "title": "ML Engineer, II - Perception",
     "absolute_url": "https://job-boards.greenhouse.io/torcrobotics/jobs/8651349002",
     "location": {"name": "Toronto, ON"}, "updated_at": "2026-10-01T10:00:00-04:00",
     "first_published": "2026-09-20T09:00:00-04:00",
     "content": "&lt;p&gt;You have 2+ years of experience with PyTorch.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;C++&lt;/li&gt;&lt;/ul&gt;"},
    {"id": 1, "title": "Account Executive", "absolute_url": "https://job-boards.greenhouse.io/torcrobotics/jobs/1",
     "location": {"name": "Dallas, TX"}, "updated_at": "2026-10-01T10:00:00Z", "content": ""},
]}

LEVER = [
    {"id": "dab396d4", "text": "Forward Deployed Software Engineer",
     "hostedUrl": "https://jobs.lever.co/palantir/dab396d4", "applyUrl": "https://jobs.lever.co/palantir/dab396d4/apply",
     "createdAt": 1758000000000, "workplaceType": "hybrid",
     "categories": {"location": "New York, NY", "allLocations": ["New York, NY", "Toronto, ON"], "team": "Dev"},
     "descriptionPlain": "Build things.", "lists": [{"text": "Requirements", "content": "<li>1+ years of industry experience</li>"}],
     "additionalPlain": "", "salaryRange": {"min": 135000, "max": 200000, "currency": "USD", "interval": "per-year-salary"}},
]

ASHBY = {"jobs": [
    {"id": "364ad643", "title": "Applied Scientist II (Audio)", "location": "New York",
     "secondaryLocations": [{"location": "Remote - US"}], "publishedAt": "2026-09-01T00:00:00.000+00:00",
     "isListed": True, "isRemote": False, "jobUrl": "https://jobs.ashbyhq.com/realitydefender/364ad643",
     "applyUrl": "https://jobs.ashbyhq.com/realitydefender/364ad643/application",
     "descriptionPlain": "PhD preferred. 3+ years of experience in audio ML.",
     "compensation": {"compensationTierSummary": "$150K – $190K", "scrapeableCompensationSalarySummary": "$150K - $190K"}},
    {"id": "hidden", "title": "Hidden Role", "location": "New York", "isListed": False, "jobUrl": "x"},
]}

SMARTRECRUITERS_LIST = {"totalFound": 1, "content": [
    {"id": "744000012345", "name": "Computer Vision Engineer", "releasedDate": "2026-10-10T08:00:00.000Z",
     "location": {"city": "Bengaluru", "region": "KA", "country": "in", "remote": False,
                  "fullLocation": "Bengaluru, KA, India"}},
]}
SMARTRECRUITERS_DETAIL = {
    "postingUrl": "https://jobs.smartrecruiters.com/BoschGroup/744000012345-computer-vision-engineer",
    "applyUrl": "https://jobs.smartrecruiters.com/oneclick-ui/company/BoschGroup/publication/x",
    "jobAd": {"sections": {"jobDescription": {"title": "Job Description", "text": "<p>Build perception.</p>"},
                           "qualifications": {"title": "Qualifications", "text": "<p>3-5 years of experience in C++</p>"}}},
}

WORKABLE = {"name": "Acme AI", "jobs": [
    {"title": "AI Engineer", "shortcode": "ABC123", "url": "https://apply.workable.com/j/ABC123",
     "application_url": "https://apply.workable.com/j/ABC123/apply", "published_on": "2026-10-05",
     "telecommuting": True, "locations": [{"city": "Mumbai", "region": "MH", "country": "India"}],
     "description": "<p>LLM agents.</p>"},
]}

RECRUITEE = {"offers": [
    {"id": 77, "slug": "ml-engineer", "title": "ML Engineer", "status": "published",
     "careers_url": "https://acme.recruitee.com/o/ml-engineer", "location": "Singapore",
     "published_at": "2026-10-02 10:00:00 UTC", "description": "<p>Ship models.</p>",
     "requirements": "<p>2+ years of professional experience</p>",
     "salary": {"min": "6000", "max": "9000", "currency": "SGD", "period": "month"}},
]}

WORKDAY_LIST = {"total": 2, "jobPostings": [
    {"title": "Senior Deep Learning Engineer", "externalPath": "/job/India-Bengaluru/Senior-DL-Engineer_JR1999",
     "locationsText": "India, Bengaluru", "postedOn": "Posted 3 Days Ago", "bulletFields": ["JR1999"]},
    {"title": "Firmware Engineer", "externalPath": "/job/US-CA-Santa-Clara/Firmware-Engineer_JR2000",
     "locationsText": "US, CA, Santa Clara", "postedOn": "Posted 30+ Days Ago", "bulletFields": ["JR2000"]},
]}
WORKDAY_DETAIL = {"jobPostingInfo": {
    "jobDescription": "<p>8+ years of experience building CUDA kernels.</p>", "location": "India, Bengaluru",
    "startDate": "2026-10-13", "canApply": True,
    "externalUrl": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/India-Bengaluru/Senior-DL-Engineer_JR1999"}}

MCF_SEARCH = {"total": 2, "results": [
    {"uuid": "abc123", "title": "AI Engineer (LLM Applications)",
     "postedCompany": {"name": "CFGI SINGAPORE PTE. LTD."},
     "salary": {"minimum": 6000, "maximum": 9000, "type": {"salaryType": "Monthly"}},
     "metadata": {"jobPostId": "MCF-2026-0123456", "newPostingDate": "2026-10-14",
                  "jobDetailsUrl": "https://www.mycareersfuture.gov.sg/job/information-technology/ai-engineer-cfgi-abc123"},
     "address": {"districts": [{"location": "Raffles Place", "region": "Central"}]}},
    {"uuid": "def456", "title": "Machine Learning Engineer",
     "postedCompany": {"name": "BEST TALENT RECRUITMENT PTE. LTD."},
     "metadata": {"newPostingDate": "2026-10-15", "jobDetailsUrl": "https://www.mycareersfuture.gov.sg/job/x-def456"},
     "address": {}},
]}
MCF_DETAIL = {"uuid": "abc123", "description": "<p>Build LLM tools for finance teams.</p>",
              "minimumYearsExperience": 3, "status": {"jobStatus": "Open"},
              "metadata": {"totalNumberJobApplication": 7, "expiryDate": "2099-11-13"}}
