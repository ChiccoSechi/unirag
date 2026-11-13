# Crawler

This page explains what the UniRAG-inf crawler downloads from the University of Cagliari's websites,
what it leaves out and why, and how it behaves. It is written both for people who run their own copy
and for the administrators of the university websites.

## Who it is

The crawler always identifies itself, in every request, with the name and the contact of **whoever
runs it** (the variables `CRAWLER_NAME`, `CRAWLER_CONTACT` and `CRAWLER_INFO_URL`):

```
User-Agent: <name> (+<page about the crawler>; <contact email>)
From: <contact email>
```

The contact is in the User-Agent too, because the standard logs of a web server do not record the
`From` header. Without a valid contact the crawler does not start. It never uses a browser
User-Agent or any other trick to look like a person.

## What it downloads

- **Course pages** on `web.unica.it` (the `crs_60_73` section), following the course menu:
  presentation, enrolment, teaching, exams, graduation, lessons, organisation, tutors, offices.
- **Notices** of the course: the 20 most recent ones, with their attachments.
- **PDF documents** linked from those pages, only of the two cohorts currently enrolled: course
  regulations, study plans, lesson timetable, exam calendar, elective exams, admission call.
- **Course descriptions** from the official course catalogue (`unica.coursecatalogue.cineca.it`):
  the courses taught in the current academic year, through three addresses of its public JSON
  service and nothing else. If this year's description is not filled in yet, last year's is used,
  and the answer says so.
- One page of the Faculty of Sciences about access to Master's degrees, and the pages linked at the
  bottom of "Other training activities" (short courses and projects that give credits).

## What it leaves out, and why

| Left out | Why |
| --- | --- |
| Emails, phone numbers, postal addresses of people | Personal data: replaced with a note such as "[email on the website]". Of professors only name and role are kept |
| Names of students (e.g. student representatives) | Personal data |
| Rankings of admission calls, attachments of graduation calendars | They contain names, dates of birth or thesis titles |
| Professors' personal pages, the university directory | Personal data, outside the course |
| Minutes of the course council, quality reports, Erasmus pages | Internal documents or outside the scope of the bot |
| Pages listing every classroom and laboratory of the Faculty | They confused the answers: lesson rooms come from the timetable |
| Past events of "Other training activities" | Only recent, ongoing and upcoming events are kept |
| Documents of older academic years, past course descriptions | Only the two cohorts currently enrolled matter |
| Esse3 and Easystaff | Their `robots.txt` forbids automatic access |
| The English version of the site, the rest of the university portal | Outside the scope of the course |

The crawler **never** uses the internal search, never logs in and never guesses URLs or document
IDs: it only follows links it finds.

## How it behaves

- **When.** The full run is every **Sunday at 01:00 UTC** (03:00 in Italy in summer, 02:00 in winter).
  From Monday to Saturday at 05:00 UTC a short run reads only the first page of the notices and
  downloads the new ones: usually 1–3 requests.
- **How fast.** **One request at a time**, with a pause of exactly **1 second** between two requests.
  A full run is capped at 400 requests.
- **When it stops.** At once on HTTP **429** or **503**, without retrying, and after 5 failed
  requests in a row (timeouts, network errors, 5xx). If the course menu is missing (for example a
  maintenance page), nothing is updated and the bot keeps last week's content.
- **From where.** From GitHub Actions.

During development the crawler runs in a **reduced mode** (`CRAWLER_FULL=0`, the default): a few
pages and at most 25 requests, saved on your computer in `cache/` (ignored by git) so that you do not
download the same page twice while you work. The full mode is meant for GitHub Actions.

## robots.txt and the local rules

At every run the crawler reads the `robots.txt` of the site and obeys it (with
[protego](https://github.com/scrapy/protego)), including rules written for its name; the same for the
course catalogue and for the few external pages it reads (on `www.unica.it`, for example). On top of
that it applies its own, stricter rules, all in `pipeline/sources.yaml`: `local_robots` (paths it never
visits), `excluded_pages` (single pages) and `excluded_sites` (whole sites).

To stop the crawler from the website side, a `robots.txt` rule is enough:

```
User-agent: UniRAG-inf
Disallow: /
```

The rule stops every copy of the project, whatever comes after `UniRAG-inf` in its name.

## Contact and requests

Every request carries the email of whoever runs that copy of the crawler, in the User-Agent and in the
`From` header. To exclude a page or a document, to slow the crawler down or to stop it, write to that
address: requests from the university are applied at the next run, without discussion.

## Updates and academic years

- Only the two cohorts currently enrolled are covered. **Regulations** and **study plans** depend on
  the year of enrolment: the bot reads both versions and, when they differ, gives both answers, each
  with its year. **Timetable, exam calendar, notices and course descriptions** are those of the
  current academic year. The year changes in September.
- **Notices** expire 7 days after the latest date written in them (or on 31 December of their year
  if they have no dates).
- A page that disappears from the site is removed only after it has been missing for **two full runs
  in a row** (normally a week; the daily notices runs do not count), so a temporary outage deletes
  nothing.

## Ownership of the content

All the content belongs to the **University of Cagliari**. It is **not** part of this repository and
is not covered by its license. During a run, pages and PDFs are downloaded on the temporary machine of
GitHub Actions and discarded when the run ends: they are never stored in the repository, in a cache or
in the artifacts of the workflows. What is kept, on Cloudflare, is only what the bot needs to answer:
the text split into short passages and the whole tables of the PDFs, each with a link to the original
page. Every answer cites its sources.

If you run your own copy, you are responsible for your crawler: use your own name and contact, keep
the pause and the limits, and apply any request from the university at the next run.
