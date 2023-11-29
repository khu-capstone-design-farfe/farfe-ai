from fastapi import FastAPI, File, UploadFile, Form
from classes import *
from setting import *
from pyannote.audio import Pipeline
import torch
from speechbrain.pretrained import SepformerSeparation as separator
import diariazation
import soundfile as sf
import os
import httpx
import asyncio


pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.0",
  use_auth_token=tk)
pipeline.to(torch.device("cuda"))
if num_speaker==2:
    separation_model = separator.from_hparams(source="speechbrain/sepformer-whamr", savedir='pretrained_models/sepformer-whamr,run_opts={"device":"cuda"}')
if num_speaker==3:
    separation_model = separator.from_hparams(source="speechbrain/sepformer-wsj03mix", savedir='pretrained_models/sepformer-wsj03mix',run_opts={"device":"cuda"})
enh_model = separator.from_hparams(source="speechbrain/sepformer-wham16k-enhancement", savedir='pretrained_models/sepformer-wham16k-enhancement',run_opts={"device":"cuda"})

app = FastAPI()

async def request(client, URI, upload=None, obj=None, header=None,json=None):
    resp = await client.post(URI, files=upload, json=json, data=obj, timeout=None, headers=header)
    cat = resp.json()
    return cat


@app.get("/")
#async def root():
def root():
    return {"message" : "Hello, world!"}

aa = [Message(**{'seq':x,'speaker':"0",'startTime':10.1,'endTime':20.1,'mix':False}) for x in range(10)]

@app.post("/api/test")
async def test(seq:int=Form(), user:str=Form(),file:UploadFile=File()):
    contents = await file.read()
    async with httpx.AsyncClient() as client:
        tasks = [request(client, ASR_URIS[i.seq%3],upload={'file':contents}, obj={"seq" : i.seq, "user" : user})
                 for i in aa]
        result = await asyncio.gather(*tasks)
    for i in result:
        aa[i['seq']].message = i['message']
    print(aa)
    return result

@app.post("/api/record")
async def records(fileName:str=Form(), user:str=Form(),
                  speakerNum:int=Form(), file: UploadFile=File()):
    global UPLOAD_DIRECTORY
    file_extension = fileName.split('.')[-1]
    contents = await file.read()
    new_filename = user + '_recordfile.' + file_extension
    file_path = os.path.join(UPLOAD_DIRECTORY, new_filename)
    with open(file_path, "wb") as fp:
        fp.write(contents)
    fileinfo = VoiceFile(user, speakerNum, file_path)
    #diar_result = aa # !////
    diar_result = diariazation.split_audios(fileinfo, pipeline, separation_model, enh_model)
    tempfilename = os.path.join(TEMP_DIRECTORY,file_path.split('/')[-1].split('.')[0]+'_temp.wav')
    async with httpx.AsyncClient() as client:
        tasks = []
        for i in diar_result:
            sf.write(tempfilename, i.audio, 1600, format="WAV")
            with open(tempfilename, 'rb') as fp:
                ct = fp.read()
            ct = contents #!!!
            tasks.append(request(client, ASR_URIS[i.seq%3], upload={'file':ct}, obj={"seq" : i.seq, "user" : user}))
        result = await asyncio.gather(*tasks)
    for i in result:
        diar_result[i['seq']].message = i['message']
    # clova sentiment
    # 1회 호출시 최대 1000자 이므로 자르기
    sentence_all = ".|".join([x.message for x in diar_result])
    sentences = []
    while len(sentence_all) > 1000:
        st = "|".join(sentence_all[:1000].split("|")[:-1])
        sentence_all = sentence_all[len(st)+1:]
        sentences.append(st)
    sentences.append(sentence_all)
    async with httpx.AsyncClient() as client:
        tasks = [request(client, CLOVA_URI, json={'content':st},
                header = CLOVA_HEADERS) for st in sentences]
        result = await asyncio.gather(*tasks)
    part_all = np.array([0.0, 0.0, 0.0, 0])
    sentiments = []
    temp_sentiment = np.array([0.0, 0.0, 0.0, 0])
    for i in result:
        i_all = i['document']['confidence']
        part_all += [i_all['positive'], i_all['negative'], i_all['neutral'], 1]
        for j in i['sentences']:
            if j['content'][0] == '|':
                sentiments.append(temp_sentiment[:3]/temp_sentiment[3])
                temp_sentiment = np.array([0.0, 0.0, 0.0, 0])
            sent = j['confidence']
            temp_sentiment += [sent['positive'],sent['negative'],sent['neutral'],1]
            if j['content'].count("|") >= 2:
                for _ in range(j['content'].count("|")-1):
                    sentiments.append(np.array([sent['positive'],sent['negative'],sent['neutral']]))
        sentiments.append(temp_sentiment[:3]/temp_sentiment[3])
        temp_sentiment = np.array([0.0, 0.0, 0.0, 0])
    part_all = part_all[:3]/part_all[3]
    for ind, data in enumerate(sentiments):
        diar_result[ind].positive = data[0]
        diar_result[ind].negative = data[1]
        diar_result[ind].neutral = data[2]
    message = [res_Content(**(x.dict())) for x in diar_result]

    # GPT summary
    try:
        sentence_all = "".join([x.message for x in diar_result])
        async with httpx.AsyncClient() as client:
            ct = "다음 통화 내용을 한 문장으로 요약해줘.\n" + sentence_all
            tasks = [request(client, GPT_URI, json={"model" : "gpt-3.5-turbo",
                                                        "messages" :[{"role":"user", "content": ct}]},
                    header = GPT_HEADER)]
            result = await asyncio.gather(*tasks)
        summary = result[0]['choices'][0]['message']['content']
    except:
        summary = "에러나서 안돌아옴요."
    res_dict = {
        "fileName" : fileName,
        "user" : user,
        "speakerNum" : speakerNum,
        "length" : len(message),
        "positive" : part_all[0],
        "negative" : part_all[1],
        "neutral" : part_all[2],
        "summary" : summary,
        "message" : message
    }
    return AudioResponse(**res_dict)