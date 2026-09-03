AISHA AVATAR SPRITES
====================

Drop anime PNG images here (transparent background, square-ish portrait, e.g.
512x512 or 768x768) and Aisha's face will use them automatically. Until you add
them, she shows a clean built-in stylised face that already blinks, lip-syncs,
and emotes — so nothing is required to get started.

FILES (only neutral.png is required; the rest are optional and improve realism):

  neutral.png        Default face, mouth CLOSED. (required to switch to sprites)
  neutral_open.png   Same face, mouth OPEN. Used for lip-sync while she speaks.
  blink.png          Same face, eyes CLOSED. Shown briefly for blinking.

  happy.png          Smiling / cheerful expression.
  thinking.png       Looking up / focused expression.
  listening.png      Attentive expression.
  sad.png            Soft / empathetic expression.
  surprised.png      Wide-eyed expression.

  <name>_open.png    Optional open-mouth variant for any expression above
                     (e.g. happy_open.png) for better lip-sync in that mood.

TIPS FOR CONSISTENCY (same girl across all files):
  - Generate one base portrait you like first (neutral.png).
  - Then use image-to-image / same seed + prompt, changing ONLY the mouth or
    eyes, to make neutral_open.png and blink.png. This keeps the same face.
  - Any anime generator works (NovelAI, Stable Diffusion, etc.). Keep the head
    in the same position/size in every image so they line up.

HOW IT ANIMATES:
  - Mouth opens/closes in real time with her actual voice (lip-sync).
  - Blinks every few seconds.
  - Expression follows her state/emotion (listening, thinking, happy, ...).
  - Gentle idle breathing/sway when quiet.
