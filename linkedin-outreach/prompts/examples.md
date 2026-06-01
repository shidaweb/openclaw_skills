# Few-shot examples

## Example 1 (ja, 良い例)

Lead:
- 田中健一 / 医療法人さくらクリニック 理事長
- 最近の投稿: "クリニック開業3年目、スタッフ定着が最大の課題"
- 業界: ヘルスケア / 従業員 25名

Output:
```json
{
  "subject": "スタッフ定着のお話、共感いたしました",
  "body": "田中先生\n\n先日のクリニック開業3年目とスタッフ定着の投稿、大変共感しながら拝読しました。25名規模で理事長業務と現場の両方を見られている中での課題、いま多くの医療法人が同じ局面に立たれていると感じます。\n\nCellCloudでは、ヘルスケアSMB特化でスタッフのオンボーディング・継続教育を仕組み化するプラットフォームを提供しており、20-50名規模のクリニック様で平均離職率を年間18%→7%まで改善した事例が複数ございます。御社の3年目フェーズ特有の論点（属人化の解消、教育コストの平準化）に直接効く構造だと思います。\n\n15分だけ、現状のヒアリングと事例共有のお時間をいただけないでしょうか。\n\n山田"
}
```

## Example 2 (ja, INSUFFICIENT_DATA)

Lead:
- 山田太郎 / 株式会社ABC 部長
- プロフィール: (空欄)
- 最近の投稿: なし

Output:
```json
{
  "subject": "SKIP",
  "body": "INSUFFICIENT_DATA: プロフィール本文・投稿・職務経歴に固有情報がなく、personalization元となる事実が抽出できない。"
}
```

## Example 3 (en, 良い例)

Lead:
- Sarah Chen / Head of Operations, BrightPath Tutoring
- Recent post: "Scaling tutor onboarding from 30 to 200 has been our biggest growth challenge this year"
- Industry: Education / 80 employees

Output:
```json
{
  "subject": "Re: scaling tutor onboarding 30→200",
  "body": "Sarah,\n\nYour post on the 30→200 tutor onboarding ramp resonated — it's the exact inflection point where most education SMBs hit a wall, and where unit economics start to suffer if onboarding stays manual.\n\nAt CellCloud we've helped tutoring companies in your size range cut new-tutor time-to-productivity by ~40% by templating the onboarding flow and surfacing the right context per cohort. One BrightPath-shaped customer went from 6-week to 3.5-week ramp this year.\n\nWould you be open to a 15-minute conversation? Happy to share the specific playbook even if there's no fit.\n\nNorimitsu"
}
```
