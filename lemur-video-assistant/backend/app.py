import ngrok
import redis
from flask import Flask, request, stream_with_context, Response
import logging
import requests
from flask_cors import CORS, cross_origin
import time
import json

r = redis.Redis(host='localhost', port=6379, db=0)

ngrok_tunnel = "https://4bdf604b79e1.ngrok.app"  #note to update this every time you restart server
r.set('ngrok_url', ngrok_tunnel)

assembly_key = "YOUR KEY HERE"

def get_transcript(id):
    headers = {'authorization': assembly_key}
    response = requests.get(
        'https://api.assemblyai.com/v2/transcript/' + id,
        json={},
        headers=headers
    )
    return response.json()

first_transcript_flag = True

# create Flask app
app = Flask(__name__)

lemur_feedback_format = "<HEADLINE> \n\n <ul><NOTE><NOTE><NOTE></ul>"

def lemur_call(previous_responses, transcript_ids):
    formatted_previous_responses = "\n\n".join(previous_responses)
    lemur_prompt = f"""
    You are a helpful assistant that is aiding me in taking notes on this live stream. Imagine that I am a Youtuber hosting a brainstorming session about new video ideas.

    Here is the is the feedback you have given me so far:

    {formatted_previous_responses}

    Imagine that you are an amazing, creative writer. You are an attendee in a writer's room and your job is to help us to expand on ideas as we discuss. 

    Please provide me by expanding on ideas you've heard so far, or simply help us to condense the ideas you've heard into a more structured format. The writers room will be viewing these notes as the stream goes on which will help me during the session. I need quick, actionable notes that can fit in 2 sentences.
    """
    headers = {'authorization': assembly_key}
    response = requests.post(
        'https://api.assemblyai.com/lemur/v3/generate/task',
        json={'prompt': lemur_prompt, 'context': lemur_feedback_format, 'transcript_ids': transcript_ids},
        headers=headers
    )
    return response.json()

ids = []
@app.route('/', methods=['POST'])
def webhook_handler():
    try:
        stream_id = request.args.get('streamid')
        print('Stream ID: ' + stream_id)
        job_id = request.json.get('transcript_id')
        job_id_response = requests.get('https://api.assemblyai.com/v2/transcript/' + job_id, headers={'authorization': assembly_key})
        if job_id_response.json()['status'] == 'error':
            #note - need to return 200 here to prevent AssemblyAI from retrying
            return {'message': 'Webhook received, but transcript had an error: perhaps it contained no audio or text'}, 200
        print('job_id: ' + job_id)
        r.rpush(stream_id, job_id) #store the transcript ids in a list named after the stream id
    
        #read the results from redis
        assistant_completion_values = r.hvals('lemur_assistant_results')
        assistant_completion_values = [value.decode('utf-8') for value in assistant_completion_values]
        if len(assistant_completion_values) == 0:
            assistant_completion_values.append("")

        #read last 10 ids from redis and convert them into the format we need
        ids = r.lrange(stream_id, -10, -1)
        ids = [id.decode('utf-8') for id in ids]
        ids = list(set(ids))

        print("TRANSCRIPT IDS", ids)

        #call lemur using the previous responses and the most recent 10 ids - note that this number of ids could be expanded dramatically to ~100
        lemur_assistant_response = lemur_call(assistant_completion_values, ids)
        print(lemur_assistant_response)

        assistant_payload = lemur_assistant_response["response"]
        if job_id and assistant_payload:
            r.hset(f'{stream_id}_assistant_results', job_id, assistant_payload)  # store the payload in a hash specific to the stream id

        return {'message': 'Webhook received'}, 200
    except Exception as e:
        print("Error: ", e)
        # Even if there's an error, we send back a '200 OK' status to prevent retries from AssemblyAI.
        return {'message': 'Webhook received, but an internal error occurred.'}, 200

@app.route('/stream')
def stream():
    def event_stream():
        stream_id = request.args.get('streamid')
        print('Stream ID: ' + stream_id)
        while True:
            # Get the last key in the list which represents the most recent results
            last_key = r.lindex(stream_id, -1)
            if last_key:
                # Get the corresponding hash entry and send it as an update
                assistant_update = r.hget(stream_id + '_assistant_results', last_key.decode())

                if assistant_update:
                    yield f"data: {json.dumps({'assistant': {last_key.decode(): assistant_update.decode()}})}\n\n"  # SSE data format

            # Sleep for a short period to prevent CPU overload
            time.sleep(3)
    headers = {
        'Content-Type': 'text/event-stream',
        'Access-Control-Allow-Origin': 'http://localhost:3000',
        'Access-Control-Allow-Credentials': 'true'
    }
    
    response = Response(stream_with_context(event_stream()), headers=headers, mimetype="text/event-stream")
    print(response.headers)
    return response

if __name__ == "__main__":

    # start the Flask app
    app.run(port=5000)