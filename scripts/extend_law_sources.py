"""Add general housing-law excerpts from the locally verified official Civil Code text.
This is an explicit source import, never an assertion that a rule has legal approval.
"""
import hashlib
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
EXCERPTS = {
 "第一百八十八条": ["诉讼", "时效", "期间"],
 "第二百零九条": ["过户", "登记", "二手房", "产权"],
 "第二百一十五条": ["过户", "登记", "二手房", "产权"],
 "第二百三十五条": ["占有", "占用", "返还房屋"],
 "第二百三十六条": ["妨害", "排除", "相邻"],
 "第二百七十二条": ["相邻", "装修", "共有"],
 "第二百八十八条": ["相邻", "通风", "采光", "排水"],
 "第五百九十五条": ["二手房", "买卖"],
 "第六百一十五条": ["质量", "瑕疵"],
 "第六百一十七条": ["质量", "瑕疵"],
 "第七百零三条": ["租赁", "承租", "租金"],
 "第七百零五条": ["租赁", "租期"],
 "第七百零六条": ["租赁", "租房备案"],
 "第七百零八条": ["租赁", "出租"],
 "第七百一十条": ["租赁", "损耗"],
 "第七百一十二条": ["租赁", "租房维修"],
 "第七百一十三条": ["租赁", "租房维修"],
 "第七百一十六条": ["转租"],
 "第七百二十二条": ["租赁", "欠租"],
 "第七百二十三条": ["租赁", "承租"],
 "第七百二十四条": ["租赁", "承租"],
 "第七百二十五条": ["租赁", "买卖不破租赁"],
 "第七百三十四条": ["租赁", "租期届满"],
}

def main():
 text=(ROOT/"evaluation/civil-official.txt").read_text()
 path=ROOT/"configs/law_sources.json"
 sources=json.loads(path.read_text())
 for number,(article,topics) in zip([188,209,215,235,236,272,288,595,615,617,703,705,706,708,710,712,713,716,722,723,724,725,734],EXCERPTS.items()):
  sid="L_civil_"+str(number)
  match=re.search(r"(?ms)^"+article+r"[　\s].*?(?=^第[一二三四五六七八九十百千万零]+条|\Z)",text)
  if not match:raise ValueError(article)
  excerpt=re.split(r"(?m)^第[一二三四五六七八九十]+[章节]",match.group())[0].strip()
  item={"id":sid,"title":"《中华人民共和国民法典》"+article,"article":article,"text":excerpt,"url":"https://www.court.gov.cn/zixun/xiangqing/233181.html","topics":topics,"retrieved_at":"2026-11-06","legal_review":"not_reviewed","sha256":hashlib.sha256(excerpt.encode()).hexdigest()}
  sources=[s for s in sources if s['id']!=sid]+[item]
 path.write_text(json.dumps(sources,ensure_ascii=False,indent=2))
 print("Official excerpt catalog:",len(sources),"entries; applicability remains pending legal review.")

if __name__=='__main__':main()
