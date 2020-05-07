#!/usr/bin/env python3

import os
import json
import config
import matcher
from pathlib import Path
from slackeventsapi import SlackEventAdapter
from flask import Flask, request, make_response, Response


slack_secrets = config.get_secrets()
slack_api_token = slack_secrets['apiToken']
slack_signing_secret = slack_secrets['signingSecret']

app = Flask(__name__)
data_directory = Path(__file__).parent / "data"
slack_client = config.get_slack_client(slack_api_token)
slack_event_adapter = SlackEventAdapter(slack_signing_secret, "/", app)

with open(data_directory / "template.md", "r") as template_file:
    message_template = template_file.read()
with open(data_directory / "model.json", "r") as model_file:
    model = json.load(model_file)


def answer_message(event_data):
    event = event_data["event"]
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
    slack_client.chat_postMessage(
        channel=event["channel"],
        thread_ts=event["ts"],
        link_names=True,
        text=message
        )


@slack_event_adapter.on("message")
def handle_event(event_data):
    answer_message(event_data)
    return


@app.before_request
def skip_retry():
    if int(request.headers.get('X-Slack-Retry-Num', '0')):
        return make_response('', 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
