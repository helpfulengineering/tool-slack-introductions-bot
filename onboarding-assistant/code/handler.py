import os
import json
import matcher
import hashlib
import boto3
from pathlib import Path
from slackeventsapi import SlackEventAdapter
from airtable import airtable
from flask import Flask, request, make_response, Response
from unittest import mock
import slack

import googlemaps



application = Flask(__name__)
configuration = json.loads(
    boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ.get("SECRET_ARN")
        ).get("SecretString")
    )
slack_client = (
    slack.WebClient(token)
    if (token := configuration.get("slack_token")) else
    mock.MagicMock(slack.WebClient)
    )
slack_event_adapter = SlackEventAdapter(
    configuration.get("slack_signing_secret", "a"),
    "/events",
    application
    )
airtable_volunteers=airtable.Airtable(
    configuration["airtable_volunteers_base"],
    api_key=configuration["airtable_token"]
    )
airtable_mails=airtable.Airtable(
    configuration["airtable_mails_base"],
    api_key=configuration["airtable_token"]
    )
function_prefix = os.environ.get("FUNCTION_PREFIX")

data_directory = Path(__file__).parent / "data"

with open(data_directory / "elements" / "form.json", "r") as form_file:
    form = json.load(form_file)
with open(data_directory / "model.json", "r") as model_file:
    model = json.load(model_file)
with open(data_directory / "elements" / "welcome.json", "r") as welcome_file:
    welcome = json.load(welcome_file)
with open(data_directory / "elements" / "success.json", "r") as success_file:
    success = json.load(success_file)
with open(data_directory / "elements" / "introduction.json", "r") as introduction_file:
    introduction = json.load(introduction_file)
with open(data_directory / "template.md", "r") as template_file:
    message_template = template_file.read()

def airtable_filter_formula(field, value):
    return "{" + field.replace("{", r"\{").replace("}", r"\}") + "} = '" + value.replace("'", r"\'").replace("\\", "\\\\") + "'"
def airtable_create_record(table, field, value):
    return airtable_volunteers.create(table, {field: value})["id"]
def airtable_unique_records(table, field, values):
    response=[]
    for value in values:
        existing = airtable_volunteers.get(table, filter_by_formula=airtable_filter_formula(field, value))["records"]
        response += [existing[0]["id"] if existing else airtable_create_record(table, field, value)]
    return list(set(response))

def resolve_address(identifier):
    gmaps=googlemaps.Client(key=configuration["google_token"])
    result = gmaps.place(
                identifier,
                fields=["address_component", "geometry", "formatted_address"])["result"]
    return {**{
    type: component["long_name"]
    for component in result["address_components"] for type in ["postal_code",
    "administrative_area_level_2",
    "administrative_area_level_1", "locality",
    "country"] if type in component["types"]
    }, "address": result["formatted_address"], "location": [result["geometry"]["location"]["lat"], result["geometry"]["location"]["lng"]]}

def format_object(object, *arguments, **keyword_arguments):
    """Applies the `str.format()` method to a nested JSON-like object."""
    if type(object) is dict:
        return {
            format_object(key, *arguments, **keyword_arguments):
            format_object(value, *arguments, **keyword_arguments)
            for key, value in object.items()
            }
    elif type(object) is list:
        return [
            format_object(item, *arguments, **keyword_arguments)
            for item in object
            ]
    elif type(object) is str:
        return object.format(*arguments, **keyword_arguments)
    else:
        return object

def handle_form(event, context = None):
    action = event
    def extract(value):
        value = list(value.values())[0]
        if value["type"] in ("static_select", "external_select"):
            return [value["selected_option"]["value"]]
        elif value["type"] in ("multi_external_select", "checkboxes"):
            return [item["value"] for item in value["selected_options"]]
        elif value["type"] == "plain_text_input":
            return value["value"]
    state = {
        field: extract(value)
        for field, value in action["view"]["state"]["values"].items()
        }
    user = slack_client.users_info(user=action["user"]["id"])["user"]
    address = resolve_address(state["location"].pop())
    record = airtable_volunteers.create("Volunteers", {
        "Slack Handle": user["profile"]["display_name_normalized"],
        "Slack User ID": user["id"],
        "Profession": state["profession"],
        "External Organization": state.get("organization",""),
        "Weekly Capacity (new)": int(state["availability"].pop()),
        "Skills": airtable_unique_records("Skills", "Name", state["skills"]),
        "Languages": airtable_unique_records("Languages", "Language", state["languages"]),
        "Industry": airtable_unique_records("Industries", "Name", state["industries"]),

        # "Equipment": "",

        "City": address.get("locality") or address.get("administrative_area_level_2", ""),
        "Country (new)": address.get("country", ""),
        "State/Province": address.get("administrative_area_level_1", ""),
        "Zip Code": address.get("postal_code", ""),
        "Geolocation": address.get("address", ""),
        "Geocode": ", ".join(map(str, address.get("location", []))),


        "Volunteer Interest": True,
        "Timezone": user["tz_label"],
        "Experience": state["experience"],
        "Management Interest": "leadership" in state["options"],
        "Privacy Policy": "privacy" in state["options"],
        })["id"]
    airtable_mails.create("Email Addresses", {
        "Volunteer Record": record,
        "Email Address": user["profile"]["email"],
    })

    introduction_message = format_object(
        introduction,
        user=action["user"]["id"],
        skills=", ".join(state["skills"] + state["languages"]),
        experience=state["experience"]
        )
    if "privacy" in state["options"]:
        slack_client.chat_postMessage(
            channel="CUXD81R6X",
            link_names=True,
            text="",
            **introduction_message,
            username=user["profile"]["display_name"],
            icon_url=user["profile"]["image_512"]
            )

    channels = "\n".join(matcher.recommend_channels(model, " ".join(state["skills"]) + state["experience"]+state["profession"]+" ".join(state["industries"])))
    jobs = "\n".join(matcher.recommend_jobs(model, " ".join(state["skills"]) + state["experience"]+state["profession"]+" ".join(state["industries"])))
    suggestion = "*Thanks for introducing yourself!*"
    if channels:
        suggestion += "\n\nRecommended channels\n" + channels
    if jobs:
        suggestion += "\n\nRecommended jobs\n" + jobs
    suggestion += "\n\n If you have manufacturing or prototyping resources, please complete the <https://kik.to/tq|Hardware Volunteer Form> too.\n_Tip: you can also add your profession or main skill to your profile (click over <@{user}> on the top left)._".format(user=action["user"]["id"])
    slack_client.chat_postMessage(
        channel=action["user"]["id"],
        link_names=True,
        text=suggestion
        )

@application.route("/interactivity", methods=["POST"])
def handle_interactivity():
    action = json.loads(request.form["payload"])

    if action["type"] == "shortcut":
        handle_team_join({"event": action}, True)
        return ""

    elif action["type"] == "block_actions":
        if action["actions"][0]["action_id"] == "show_form":
            slack_client.views_open(
                trigger_id=action["trigger_id"],
                view=format_object(form, session=action["trigger_id"])
                )
        return ""

    elif action["type"] == "view_submission":
        boto3.client('lambda').invoke(
            FunctionName=os.environ.get("FUNCTION_PREFIX") + "form",
            Payload=json.dumps(action),
            InvocationType='Event'
            )
        return "" # return success for a new view (success.json)

    else:
        return ""


@slack_event_adapter.on("team_join")
def handle_team_join(event):
    slack_client.chat_postMessage(
        **format_object(welcome, user=event["event"]["user"]["id"]),
        channel=event["event"]["user"]["id"],
        link_names=True,
        text=""
        )
    return ""


# DEPRECATED
@slack_event_adapter.on("message")
def handle_message(event):
    event = event["event"]
    if 'bot_profile' in event:
        return
    if 'thread_ts' in event:
        return
    if 'text' not in event:
        return
    suggestion = ""
    channels = "\n".join(matcher.recommend_channels(model, event["text"]))
    jobs = "\n".join(matcher.recommend_jobs(model, event["text"]))
    if channels:
        suggestion += (
            "\n*Recommended channels*\n" + channels + "\n"
            "(#skill channels have people with similar skills in them; "
            "#discussion channels talk about a topic; #project channels "
            "are working on a project)\n"
            )
    if jobs:
        suggestion += "\n*Recommended jobs*\n{}\n".format(jobs)
    message = message_template.format(suggestion=suggestion)

    print(slack_client.chat_postMessage(
        channel=event["channel"],
        thread_ts=event["ts"],
        link_names=True,
        text=message
        ))
    return


@application.before_request
def skip_retry():
    if int(request.headers.get('X-Slack-Retry-Num', '0')):
        return make_response('', 200)


if __name__ == "__main__":
    application.run(
        host="0.0.0.0",
        port=80
        )
