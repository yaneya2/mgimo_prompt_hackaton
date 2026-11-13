"""Read-only identity lookup for excluded materials."""
import json

def excluded_metadata(storage, project_id, identities):
    found=[]
    with storage.transaction() as db:
        for identity in identities:
            row=db.execute("SELECT payload FROM entities WHERE project_id=? AND kind='source' AND id=? ORDER BY version DESC LIMIT 1",(project_id,identity)).fetchone()
            if row:
                value=json.loads(row[0]);found.append({k:value[k] for k in ('id','kind','url','title','origin_note')})
    return tuple(found)
