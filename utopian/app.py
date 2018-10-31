import json
import logging
import os
import timeago
from collections import defaultdict
from beem.comment import Comment
from beem.account import Account
from bson import json_util
from collections import Counter
from datetime import datetime, timedelta, date
from dateutil.parser import parse
from flask import Flask, jsonify, render_template, abort
from flask_cors import CORS
from flask_restful import Resource, Api
from itertools import zip_longest
from pymongo import MongoClient
from statistics import mean
from webargs import fields, validate
from webargs.flaskparser import use_args, use_kwargs, parser, abort

# Score needed for a vote
MIN_SCORE = 10

# Logging
DIR_PATH = os.path.dirname(os.path.realpath(__file__))
LOGGER = logging.getLogger("utopian-io")
LOGGER.setLevel(logging.INFO)
FH = logging.FileHandler(f"{DIR_PATH}/test.log")
FH.setLevel(logging.DEBUG)
FORMATTER = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
FH.setFormatter(FORMATTER)
LOGGER.addHandler(FH)

# Mongo and Flask
CLIENT = MongoClient()
DB = CLIENT.utempian
app = Flask(__name__)
CORS(app)
api = Api(app)


@app.template_filter("timeago")
def time_ago(date):
    return timeago.format(date)


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


@app.route("/api/moderators")
def moderaors():
    moderators = [moderator["account"] for moderator in DB.moderators.find()]
    return jsonify(moderators)


@app.route("/json/<json_file>")
def rewards(json_file):
    """Return all moderator's points for the given week."""
    filename = os.path.join(app.static_folder, "{}.json".format(json_file))
    try:
        with open(filename) as fp:
            data = json.load(fp)
        return jsonify(data)
    except:
        abort(404)


@app.route("/")
def index():
    """Sends all unreviewed contributions to index.html."""
    contributions = DB.contributions
    unreviewed = contributions.find({"status": "unreviewed"})
    unreviewed = [contribution for contribution in unreviewed]
    return render_template("index.html", contributions=unreviewed)


def without_score(contribution):
    """Returns a contribution without the score."""
    return {x: contribution[x] for x in contribution if x != "score"}


class ContributionResource(Resource):
    """Endpoint for contributions in the spreadsheet."""
    query_parameters = {
        "category": fields.Str(),
        "status": fields.Str(),
        "author": fields.Str(),
        "moderator": fields.Str(),
        "staff_picked": fields.Bool(),
        "review_status": fields.Str(),
        "url": fields.Str(),
        "voted_on": fields.Bool(),
        "repository": fields.Str(),
        "beneficiaries_set": fields.Bool(),
        "is_vipo": fields.Bool(),
    }

    @use_args(query_parameters)
    def get(self, query_parameters):
        """Uses the given query parameters to search for contributions in the
        database.
        """
        contributions = [json.loads(json_util.dumps(without_score(c)))
                         for c in DB.contributions.find(query_parameters)]
        return jsonify(contributions)


class BannedUsersResource(Resource):
    """Endpoint for banned users in the spreadsheet."""
    query_parameters = {
        "name": fields.Str(),
        "banned": fields.Bool()
    }

    @use_args(query_parameters)
    def get(self, query_parameters):
        banned_users = [json.loads(json_util.dumps(user))
                        for user in DB.users.find(query_parameters)]
        return jsonify(banned_users)


def string_to_date(input):
    """Converts a given string to a date."""
    if input == "today":
        today_date = date.today()
        return datetime(today_date.year, today_date.month, today_date.day)
    try:
        return parse(input)
    except Exception as error:
        abort(422, errors=str(error))


def average(score):
    """Returns the average score of the given list of scores."""
    try:
        return mean(score)
    except Exception:
        return 0


def percentage(reviewed, voted):
    """Returns the percentage of voted contributions."""
    try:
        return 100.0 * voted / reviewed
    except ZeroDivisionError:
        return 100.0


def moderator_statistics(contributions):
    """Returns a dictionary containing statistics about all moderators."""
    moderators = {}
    for contribution in contributions:
        if contribution["status"] == "unreviewed":
            continue
        moderator = contribution["moderator"]

        # If contribution was submitted by banned user skip it
        if moderator == "BANNED":
            continue

        # Set default in case moderator doesn't exist
        moderators.setdefault(
            moderator, {
                "moderator": moderator,
                "category": [],
                "average_score": [],
                "average_without_0": []
            }
        )

        # Append scores and categories
        moderators[moderator]["average_score"].append(contribution["score"])
        moderators[moderator]["category"].append(contribution["category"])
        if contribution["score"] > 0:
            moderators[moderator]["average_without_0"].append(
                contribution["score"])

    moderator_list = []
    for moderator, value in moderators.items():
        # Set new keys and append value to list
        value["category"] = Counter(value["category"]).most_common()
        value["average_score"] = average(value["average_score"])
        value["average_without_0"] = average(value["average_without_0"])
        moderator_list.append(value)

    return {"moderators": moderator_list}


def category_statistics(contributions, include_score=False):
    """Returns a dictionary containing statistics about all categories."""
    categories = {}
    categories.setdefault(
        "all", {
            "category": "all",
            "average_score": [],
            "average_without_0": [],
            "voted": 0,
            "not_voted": 0,
            "unvoted": 0,
            "rewardable": 0,
            "task-requests": 0,
            "moderators": [],
            "rewarded_contributors": [],
            "total_payout": 0,
            "utopian_total": [],
            "authors_vote_weights": defaultdict(list),
            "authors_scores": defaultdict(list)
        }
    )
    for contribution in contributions:
        # Don't count unreviewed contributions
        if contribution["status"] == "unreviewed":
            continue
        category = contribution["category"]
        moderator = contribution["moderator"]
        author = contribution["author"]
        score = contribution["score"]
        total_payout = contribution["total_payout"]
        utopian_vote = contribution["utopian_vote"]

        # Set default in case category doesn't exist
        categories.setdefault(
            category, {
                "category": category,
                "average_score": [],
                "average_without_0": [],
                "voted": 0,
                "not_voted": 0,
                "unvoted": 0,
                "rewardable": 0,
                "moderators": [],
                "rewarded_contributors": [],
                "total_payout": 0,
                "utopian_total": [],
                "authors_vote_weights": defaultdict(list),
                "authors_scores": defaultdict(list)
            }
        )

        # Check if contribution was voted on or unvoted
        for category in [category, "all"]:
            if contribution["status"] == "unvoted":
                categories[category]["unvoted"] += 1
                categories[category]["not_voted"] += 1
            elif score > MIN_SCORE:
                if utopian_vote > 0:
                    categories[category]["voted"] += 1
                else:
                    categories[category]["not_voted"] += 1
                categories[category]["rewardable"] += 1
            else:
                categories[category]["not_voted"] += 1

            # Add moderator, score and total payout in SBD
            categories[category]["moderators"].append(moderator)
            categories[category]["average_score"].append(score)
            categories[category]["total_payout"] += total_payout
            categories[category]["utopian_total"].append(utopian_vote)
            categories[category]["authors_vote_weights"][author].append(utopian_vote)
            if include_score:
                categories[category]["authors_scores"][author].append(score)

            if score > 0:
                categories[category]["average_without_0"].append(score)

            if score > MIN_SCORE:
                categories[category]["rewarded_contributors"].append(author)

    category_list = []
    for category, value in categories.items():
        # Set new keys and append value to list
        value["reviewed"] = value["voted"] + value["not_voted"]
        value["average_score"] = average(value["average_score"])
        value["average_without_0"] = average(value["average_without_0"])
        value["moderators"] = Counter(value["moderators"]).most_common()
        value["rewarded_contributors"] = Counter(
            value["rewarded_contributors"]).most_common()
        try:
            value["average_payout"] = value["total_payout"] / value["reviewed"]
        except ZeroDivisionError:
            value["average_payout"] = 0
        value["pct_voted"] = percentage(value["reviewed"], value["voted"])

        # Add Utopian.io's vote statistics
        value["utopian_total"] = [vote for vote in value["utopian_total"]
                                  if vote != 0]
        value["average_utopian_vote"] = average(value["utopian_total"])
        value["utopian_total"] = sum(value["utopian_total"])
        category_list.append(value)

    return {"categories": category_list}


def project_statistics(contributions):
    """Returns a dictionary containing statistics about all projects."""
    projects = {}
    for contribution in contributions:
        # Don't count unreviewed contributions
        if contribution["status"] == "unreviewed":
            continue
        project = contribution["repository"]
        utopian_vote = contribution["utopian_vote"]

        # Set default in case category doesn't exist
        projects.setdefault(
            project, {
                "project": project,
                "average_score": [],
                "average_without_0": [],
                "voted": 0,
                "not_voted": 0,
                "unvoted": 0,
                "task-requests": 0,
                "moderators": [],
                "average_payout": [],
                "total_payout": 0,
                "utopian_total": []
            }
        )

        # Check if contribution was voted on or unvoted
        if contribution["status"] == "unvoted":
            projects[project]["unvoted"] += 1
            projects[project]["not_voted"] += 1
        elif contribution["voted_on"]:
            projects[project]["voted"] += 1
        else:
            projects[project]["not_voted"] += 1

        # If contribution was a task request count this
        if "task" in contribution["category"]:
            projects[project]["task-requests"] += 1

        # Add moderator and score
        projects[project]["moderators"].append(contribution["moderator"])
        projects[project]["average_score"].append(contribution["score"])
        projects[project]["total_payout"] += contribution["total_payout"]
        projects[project]["utopian_total"].append(utopian_vote)

        if contribution["score"] > 0:
            projects[project]["average_without_0"].append(
                contribution["score"])

    project_list = []
    for project, value in projects.items():
        # Set new keys and append value to list
        value["reviewed"] = value["voted"] + value["not_voted"]
        value["average_score"] = average(value["average_score"])
        value["average_without_0"] = average(value["average_without_0"])
        value["average_payout"] = value["total_payout"] / value["reviewed"]
        value["moderators"] = Counter(value["moderators"]).most_common()
        value["pct_voted"] = percentage(value["reviewed"], value["voted"])

        # Add Utopian.io's vote statistics
        value["utopian_total"] = [vote for vote in value["utopian_total"]
                                  if vote != 0]
        value["average_utopian_vote"] = average(value["utopian_total"])
        value["utopian_total"] = sum(value["utopian_total"])
        project_list.append(value)

    return {"projects": project_list}


def staff_pick_statistics(contributions):
    """Returns a list of contributions that were staff picked."""
    staff_picks = []
    for contribution in contributions:
        # If contribution wasn't staff picked skip it
        if not contribution["staff_picked"]:
            continue

        staff_picks.append(contribution)

    return {"staff_picks": staff_picks}


def task_request_statistics(contributions):
    """Returns a list of task requests."""
    task_requests = []
    for contribution in contributions:
        # If contribution wasn't staff picked skip it
        if "task" in contribution["category"]:
            task_requests.append(contribution)

    return {"task_requests": task_requests}


class WeeklyResource(Resource):
    """Endpoint for weekly contribution data (requested)."""
    def get(self, date):
        LOGGER.info(f"Retrieving for {date}")
        # Get date for retrieving posts
        date = string_to_date(date)
        week_ago = date - timedelta(days=7)

        # Retrieve contributions made in week before the given date
        contributions = DB.contributions
        pipeline = [
            {"$match": {"review_date": {"$gte": week_ago, "$lt": date}}}]
        contributions = [json.loads(json_util.dumps(c))
                         for c in contributions.aggregate(pipeline)]

        moderators = moderator_statistics(contributions)
        categories = category_statistics(contributions)
        projects = project_statistics(contributions)
        staff_picks = staff_pick_statistics(contributions)
        task_requests = task_request_statistics(contributions)

        return jsonify(
            [moderators, categories, projects, staff_picks, task_requests])


def convert(contribution):
    del contribution["_id"]
    contribution["voting_weight"] = exponential_vote(contribution)
    contribution["created"] = str(contribution["created"])
    contribution["review_date"] = str(contribution["review_date"])
    return contribution


def batch_comments(contributions):
    """Get all comments to be upvoted."""
    sorted_by_review = sorted(contributions, key=lambda x: x["review_date"])
    for contribution in sorted_by_review:
        if (contribution["review_date"] != datetime(1970, 1, 1) and
                contribution["review_status"] == "pending"):
            oldest = contribution["review_date"]
            break

    if oldest > datetime.now() - timedelta(days=2):
        return []

    batch = [c for c in sorted_by_review
             if c["review_date"] <= oldest + timedelta(days=1) and
             c["review_date"] <= datetime.now() - timedelta(days=2) and
             c["review_status"] == "pending"]
    return batch


def batch_contributions(contributions):
    """Get all contributions to be upvoted."""
    return [c for c in contributions if c["status"] == "pending"]


class BatchResource(Resource):
    """Endpoint for the posts to be voted in a batch."""
    def get(self, batch_type):
        all_contributions = [c for c in DB.contributions.find({
            "$or": [
                {"status": "pending"},
                {"review_status": "pending"}
            ]
        })]

        if batch_type == "comments":
            batch = batch_comments(all_contributions)
        elif batch_type == "contributions":
            batch = batch_contributions(all_contributions)
        else:
            return jsonify({})
        eligible = [json.loads(json_util.dumps(convert(c))) for c in batch]
        return jsonify(eligible)


api.add_resource(WeeklyResource, "/api/statistics/<string:date>")
api.add_resource(BannedUsersResource, "/api/bannedUsers")
api.add_resource(ContributionResource, "/api/posts")
api.add_resource(BatchResource, "/api/batch/<string:batch_type>")


def intro_section(first_day, last_day):
    """Creates the introduction section / headline for the Utopian weekly post.

    The week is defined by the first and last days of the week.
    """
    LOGGER.info("Generating post introduction section...")
    section = (
        f"# Weekly Top of Utopian.io: {first_day:%B} {first_day.day} - "
        f"{last_day:%B} {last_day.day}"
        "<br><br>[Introduction (summary of the week)]"
    )
    return section


def footer_section():
    """Creates the footer section for the Utopian weekly post."""
    LOGGER.info("Generating post footer section...")
    section = (
        "![divider](https://cdn.steemitimages.com/DQmWQWnJf7s671sHmGdzZVQMqEv7DyXL9qknT67vyQdAHfL/utopian_divider.png)"
        "<br><br>## First Time Contributing in [Utopian.io](https://join.utopian.io/)?"
        "<br><br>&lt;a href=&quot;https://join.utopian.io/guidelines&quot;&gt;Learn how to contribute on our website&lt;/a&gt;"
        "<br><br>&lt;center&gt;&lt;iframe width=&quot;560&quot; height=&quot;315&quot; src=&quot;https://www.youtube.com/embed/8S1AtrzYY1Q&quot; frameborder=&quot;0&quot; allow=&quot;autoplay; encrypted-media&quot; allowfullscreen&gt;&lt;/iframe&gt;&lt;/center&gt;"
        "<br><br>&lt;center&gt;&lt;a href=&quot;https://discord.gg/h52nFrV&quot;&gt;&lt;img src=&quot;https://cdn.discordapp.com/attachments/396653220702978049/452918421235957763/footer_558.png&quot; /&gt;&lt;/a&gt;&lt;/center&gt;"
        "<br><br>&lt;center&gt;&lt;h4&gt;&lt;a href=&quot;https://steemconnect.com/sign/account-witness-vote?witness=utopian-io&amp;approve=1&quot;&gt;Vote for the Utopian Witness&lt;/a&gt;&lt;/h4&gt;&lt;/center&gt;"
    )
    return section


def staff_pick_section(staff_picks):
    """Creates the staff pick section for the Utopian weekly post."""
    LOGGER.info("Generating staff pick statistics section...")
    section = "## Staff Picks"
    for staff_pick in staff_picks["staff_picks"]:
        url = staff_pick["url"]
        post = Comment(url)
        title = post.json()["title"]

        # If title can't be retrieved set it to the post's URL
        if not title:
            title = url
        author = staff_pick['author']
        category = staff_pick['category']

        # Add staff pick to the string
        section += (
            f"<br><br>### &lt;a href='{url}'&gt;{title}&lt;/a&gt; by @{author} "
            f"[{category}]<br><br>[Image (contributor profile image / image from "
            "the post)]<br><br>[Paragraph: Background info on project etc.]"
            "<br><br>[Paragraph: CM review, including etc.]<br><br>"
            f"Total payout: {staff_pick['total_payout']:.2f} STU<br>"
            f"Number of votes: {staff_pick['total_votes']}"
        )

    return section


def post_statistics_section(categories, contributions):
    """Creates the post statistics part for the Utopian weekly post."""
    LOGGER.info("Generating post statistics section...")
    section = (
        "## Utopian.io Post Statistics<br><br>"
        "The staff picked contributions are only a small (but exceptional) "
        "example of the mass of contributions reviewed and rewarded by "
        "Utopian.io.<br><br>"
    )

    # Get some statistics needed
    for category in categories["categories"]:
        reviewed = category["reviewed"]
        voted = category["voted"]
        utopian_total = category["utopian_total"]
        average_vote = category["average_utopian_vote"]
        if category["category"] == "all":
            break

    # Get contributions with highest payout and engagement
    highest_payout = sorted(
        contributions, key=lambda x: x["total_payout"], reverse=True)[0]
    most_engagement = sorted(
        contributions, key=lambda x: x["total_votes"], reverse=True)[0]
    title = Comment(most_engagement["url"]).title

    # Create the section with the above statistics
    section += (
        f"* Overall, the last week saw a total of {reviewed} posts, with "
        f"{voted} of them rewarded through an upvote by @utopian-io.<br>"
        "* In total, Utopian.io distributed an approximate of "
        f"{utopian_total:.2f} STU to contributors.<br>"
        "* The highest payout seen on any Utopian.io contribution this week "
        f"was {highest_payout['total_payout']} STU, with a total of "
        f"{highest_payout['total_votes']} votes received from the community."
        "<br>* The contribution that attracted the most engagement was "
        f"&lt;a href='{most_engagement['url']}'&gt;{title}&lt;/a&gt;, with no "
        f"less than {most_engagement['total_comments']} comments in its "
        "comment threads.<br>"
        f"* The average vote given by Utopian.io was worth {average_vote:.2f} "
        "STU.<br><br>## Category Statistics<br><br>"
        "|Category|Reviewed|Rewardable|Rewarded|Total rewards|Top contributor|<br>"
        "|:-|:-|:-|:-|-:|:-|"
    )

    # Create the table with category statistics
    for category in categories["categories"]:
        # Skip if category is 'all' or is task
        if category["category"] == "all" or "task" in category["category"]:
            continue

        # Don't include category is no contributions were rewarded
        rewarded = category["voted"]
        rewardable = category["rewardable"]
        if rewardable == 0:
            continue

        # Get all the data needed
        reviewed = category["reviewed"]
        rewards = f"{category['utopian_total']:.2f}"
        scores_per_author = category['authors_scores']
        weights_per_author = category['authors_vote_weights']
        author = f"@{sorted(scores_per_author, key=lambda x: (sum([score**2 for score in scores_per_author[x]]), sum(weights_per_author[x])), reverse=True)[0]}"
        category = category["category"]

        # Add the row
        section += (
            f"<br>|{category}|{reviewed}|{rewardable}|{rewarded}|{rewards} STU|{author}|")

    return section


@app.route("/weekly", defaults={"date": "today"})
@app.route("/weekly/<date>")
def weekly(date):
    """Returns weekly statistics in a format that can be posted on Steemit."""
    today = string_to_date(date)
    week_ago = today - timedelta(days=7)
    contributions = DB.contributions
    pipeline = [
        {"$match": {"review_date": {"$gte": week_ago}}}]
    contributions = [json.loads(json_util.dumps(c))
                     for c in contributions.aggregate(pipeline)]

    # Get the data needed for all statistics
    categories = category_statistics(contributions, include_score=True)
    staff_picks = staff_pick_statistics(contributions)

    # Get each section of the post
    try:
        post_intro_section = intro_section(week_ago, today)
        staff_section = staff_pick_section(staff_picks)
        post_section = post_statistics_section(categories, contributions)
        post_footer_section = footer_section()
    except Exception as error:
        LOGGER.error(error)
        body = ("No statistics to show for this week ("
                f"{week_ago:%B} {week_ago.day} - {today:%B} {today.day}).")
    else:
        body = "<br><br>".join([post_intro_section, staff_section,
                                post_section, post_footer_section])
        LOGGER.info(body)
    return render_template("weekly.html", body=body)


def update_vp(current_vp, updated, recharge_time):
    seconds = (datetime.now() - updated).total_seconds()
    regenerated_vp = seconds * 10000 / 86400 / 5 / 100

    # Update recharge_time
    try:
        recharge_time = parse(recharge_time)
        recharge_time = timedelta(
            hours=recharge_time.hour,
            minutes=recharge_time.minute,
            seconds=recharge_time.second)
        recharge_time = recharge_time - timedelta(seconds=seconds)
        if recharge_time < timedelta(seconds=1):
            recharge_time = "0:00:00"
    except ValueError:
        pass

    current_vp += regenerated_vp
    current_vp = 100 if current_vp > 100 else f"{current_vp:.2f}"

    return float(current_vp) - 0.01, str(recharge_time).split(".")[0]


def account_information():
    accounts = DB.accounts
    account = accounts.find_one({"account": "utopian-io"})
    updated = account["updated"]
    current_vp, recharge_time = update_vp(
        account["current_vp"], updated, account["recharge_time"])

    return (
        current_vp, recharge_time, account["recharge_class"])


MAX_VOTE = {
    "ideas": 20.0,
    "development": 55.0,
    "bug-hunting": 13.0,
    "translations": 35.0,
    "graphics": 40.0,
    "analysis": 45.0,
    "social": 30.0,
    "documentation": 30.0,
    "tutorials": 30.0,
    "video-tutorials": 35.0,
    "copywriting": 30.0,
    "blog": 30.0,
    "anti-abuse": 50.0,
    "iamutopian": 50.0,
}
MAX_TASK_REQUEST = 6.0
EXP_POWER = 2.1


def exponential_vote(contribution):
    """Calculates the exponential vote for the bot."""
    score = contribution["score"]
    category = contribution["category"]

    is_vipo = contribution["is_vipo"]
    beneficiaries_set = contribution["beneficiaries_set"]
    try:
        max_vote = MAX_VOTE[category]
    except:
        max_vote = MAX_TASK_REQUEST

    power = EXP_POWER
    weight = pow(
        score / 100.0,
        power - (score / 100.0 * (power - 1.0))) * max_vote

    def update_weight(weight):
        """Updates the voting percentage if beneficiaries utopian.pay set."""
        weight = float(weight)
        new_weight = weight + 0.2 * weight + 5.0 / 100.0 + 1.0
        return new_weight

    if beneficiaries_set:
        weight = update_weight(weight)

    if is_vipo:
        weight *= 1.2

    return weight


STEEM_100_PERCENT = 10000
STEEM_VOTING_MANA_REGENERATION_SECONDS = 432000


def estimate_vote_time(contributions, recharge_time):
    """Estimates the vote time of the given contributions."""
    for i, contribution in enumerate(contributions):
        if "score" not in contribution.keys():
            continue
        if i == 0:
            hours, minutes, seconds = [int(x) for x in
                                       recharge_time.split(":")]
            vote_time = datetime.now() + timedelta(
                hours=hours, minutes=minutes, seconds=seconds)
            contribution["vote_time"] = vote_time
            continue
        missing_vp = 2 * exponential_vote(contribution) / 100.0
        recharge_seconds = (missing_vp * 100 *
                            STEEM_VOTING_MANA_REGENERATION_SECONDS /
                            STEEM_100_PERCENT)
        vote_time = vote_time + timedelta(seconds=recharge_seconds)
        contribution["vote_time"] = vote_time
    return contributions


@app.route("/queue")
def queue():
    contributions = DB.contributions
    pending = [contribution for contribution in
               contributions.find({"status": "pending"})]

    valid = []
    invalid = []

    current_vp, recharge_time, recharge_class = account_information()

    for contribution in pending:
        valid.append(contribution)
        contribution["valid_age"] = True
        created = datetime.now() - contribution["created"]
        time_until_expiration = timedelta(days=6, hours=12) - created
        if time_until_expiration < timedelta(hours=12):
            contribution["nearing_expiration"] = True
            until_expiration = datetime.now() + time_until_expiration
            contribution["until_expiration"] = until_expiration

            try:
                t = datetime.strptime(recharge_time, "%H:%M:%S")
                time_until_vote = timedelta(
                    hours=t.hour, minutes=t.minute, seconds=t.second)
            except:
                continue
            if time_until_expiration < time_until_vote:
                contribution["will_expire"] = True

    valid = sorted(valid, key=lambda x: x["created"])
    invalid = sorted(invalid, key=lambda x: x["created"])

    if not recharge_time:
        recharge_time = "0:0:0"

    contributions = estimate_vote_time((valid + invalid), recharge_time)

    return render_template(
        "queue.html", contributions=contributions, current_vp=current_vp,
        recharge_time=recharge_time, recharge_class=recharge_class)


@app.route("/comments")
def moderator_comments():
    contributions = DB.contributions
    pending_comments = [contribution for contribution in
                        contributions.find({"review_status": "pending"})]
    pending_contributions = [contribution for contribution in
                             contributions.find({"status": "pending"})]

    valid = []
    invalid = []

    for contribution in pending_comments:
        if (datetime.now() - timedelta(days=2)) > contribution["review_date"]:
            valid.append(contribution)
            contribution["valid_age"] = True
        else:
            invalid.append(contribution)
            contribution["valid_age"] = False

    valid = sorted(valid, key=lambda x: x["created"])
    invalid = sorted(invalid, key=lambda x: x["created"])

    current_vp, recharge_time, recharge_class = account_information()

    if not recharge_time:
        recharge_time = "0:0:0"

    contributions = estimate_vote_time(pending_contributions, recharge_time)

    comments = [c for c in sorted((valid + invalid),
                key=lambda x: x["review_date"])
                if c["moderator"] not in ["ignore", "irrelevant", "banned"] or
                c["comment_url"] != ""]

    for comment, contribution in zip_longest(comments, contributions):
        if not comment:
            continue
        if contribution:
            if "vote_time" not in contribution.keys():
                continue
            comment["vote_time"] = contribution["vote_time"]
        else:
            comment["vote_time"] = "TBD"

    return render_template(
        "comments.html", contributions=comments, current_vp=current_vp,
        recharge_time=recharge_time, recharge_class=recharge_class)


@app.context_processor
def inject_last_updated():
    categories = sorted(["analysis", "tutorials", "graphics", "copywriting",
                         "development", "blog", "ideas", "social", "all",
                         "bug-hunting", "video-tutorials", "translations",
                         "anti-abuse", "iamutopian"])
    account = DB.accounts.find_one({"account": "utopian-io"})
    return dict(last_updated=account["updated"].strftime("%H:%M %Z"),
                categories=categories)


def main():
    app.run(host="0.0.0.0")


if __name__ == '__main__':
    main()
