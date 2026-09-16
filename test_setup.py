import insightface
app = insightface.app.FaceAnalysis(name='buffalo_l')
app.prepare(ctx_id=-1)   # -1 = use CPU
print("SUCCESS — everything is installed and the model loaded.")
