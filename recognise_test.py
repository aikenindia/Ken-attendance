import insightface, numpy as np, cv2
import tkinter as tk
from tkinter import filedialog

app = insightface.app.FaceAnalysis(name='buffalo_l')
app.prepare(ctx_id=-1)

# hide the small empty tkinter window
tk.Tk().withdraw()

def get_numbers(path):
    img = cv2.imread(path)
    if img is None:
        print("  Cannot read file:", path)
        return None
    faces = app.get(img)
    if not faces:
        print("  No face found in", path)
        return None
    return faces[0].normed_embedding

# --- ENROL: pick 5 photos of the SAME person ---
print("Select 5 photos of the SAME person to enrol...")
enrol_paths = filedialog.askopenfilenames(
    title="Select 5 photos of the SAME person",
    filetypes=[("Images", "*.jpg *.jpeg *.png")]
)

vecs = []
for p in enrol_paths:
    v = get_numbers(p)
    if v is not None:
        vecs.append(v)

if not vecs:
    print("No usable enrolment photos found.")
    exit()

saved = np.mean(vecs, axis=0)
print("Enrolled from", len(vecs), "photos\n")

# --- TEST: pick ONE photo to check ---
def test():
    print("Select ONE photo to test...")
    path = filedialog.askopenfilename(
        title="Select a photo to test",
        filetypes=[("Images", "*.jpg *.jpeg *.png")]
    )
    if not path:
        print("  No file selected")
        return
    new = get_numbers(path)
    if new is None:
        return
    score = float(np.dot(new, saved))
    result = "SAME PERSON" if score > 0.4 else "different"
    print(path.split("/")[-1], "-> score", round(score, 2), "=>", result)

# test twice — once with same person, once with a different person
test()   # pick a photo of the SAME person
test()   # pick a photo of a DIFFERENT person