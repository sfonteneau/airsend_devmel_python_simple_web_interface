import flask
import json
import requests
import os
from flask import Flask, request, Response, redirect, render_template
from waitress import serve
from iniparse import RawConfigParser

app = flask.Flask(__name__)

dirname = os.path.dirname(os.path.realpath(__file__))

with open('%s/AirSend.json' % dirname,'r') as f :
    datajson = json.loads(f.read())['devices']

with open('%s/groupes.json' % dirname,'r') as f :
    groupes_volet = json.loads(f.read())

dict_groupes={}
for g in groupes_volet:
    dict_groupes[g['name']]=g

inifile = RawConfigParser()
inifile.read('%s/config.ini' % dirname)
ip_airsend = inifile.get('global', 'ip_airsend')
password_airsend = inifile.get('global', 'password_airsend')
airsendwebservice = inifile.get('global', 'airsendwebservice')

@app.route("/individuel")
def individuel():
    return render_template("index.html.j2",datajson=datajson)

@app.route("/")
def groupes():
    return render_template("index.html.j2",datajson=groupes_volet)

@app.route("/api/action",methods=['POST'])
def api_action(): 
    if request.headers.get('Content-Encoding') == 'gzip':
        raw_data = zlib.decompress(request.data)
    else:
        raw_data = request.data

    dataapi = json.loads(raw_data)

    headers = {
            'accept': 'application/json',
            'Content-Type' : 'application/json',
            'Authorization': 'Bearer sp://%s@[%s]?gw=0' % (password_airsend,ip_airsend)
    }

    if dataapi['voletId'] in dict_groupes :
        sendto= dict_groupes[dataapi['voletId']]['entries']
    else:
        sendto = [dataapi['voletId']]

    for entry in datajson:
        if not entry['name'] in sendto:
            continue
        data = {"wait": False, "channel": {"id":entry['pid'],"source":entry['addr']}, "thingnotes":{"notes":[{"method":"PUT","type":"STATE","value":dataapi['action'] }]}}
        requests.post("%s/airsend/transfer" % airsendwebservice,headers=headers,data=json.dumps(data))
    return "OK"

if __name__ == "__main__":
#    app.run(port="8889", debug=True)
    serve(app, host='127.0.0.1', port='8889', threads=150)

