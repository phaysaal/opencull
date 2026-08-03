# OpenCull photographic decision policy

OpenCull separates perception from selection. The first vision pass describes
what is visibly happening without choosing winners. A second, independent
vision model acts as the curator and compares those observations with the
images and scanner measurements.

## Priority order

1. **Human moment and documentary value**
   - authentic emotion, relationship, interaction, and a unique family moment;
   - a child's unusual but appealing smile or laugh can justify keeping an
     additional frame;
   - technical imperfection does not automatically defeat an irreplaceable
     moment.
2. **Readiness and timing**
   - accidental blink, half-blink, caught mid-speech, visibly unready subject,
     interrupted gesture, or a transitional pose;
   - distinguish an awkward accidental open mouth from a natural smile, laugh,
     exclamation, singing, or other contextually coherent expression.
3. **Pose, expression, and subject appearance**
   - flattering and natural head, chin, shoulder, hand, and body positions;
   - gaze and expression coherence across subjects;
   - never assume that open eyes or a closed mouth are universally better.
4. **Hard-to-repair visual defects**
   - missed focus on the important face, motion blur across facial features,
     blocked eyes or face, severe edge crop through a person, or an unrecoverable
     highlight over important facial detail.
5. **Surroundings and accidental interference**
   - distracting people, objects apparently growing from a head, unwanted
     reflections, clutter, signs, vehicles, bins, cut limbs, and competing
     highlights;
   - distinguish genuine context from removable or avoidable distraction.
6. **Composition and visual coherence**
   - balance, subject separation, gesture, spacing between people, layers,
     camera angle, and whether the frame communicates the moment clearly.
7. **Usually recoverable RAW-development issues**
   - moderate exposure error, white balance, highlight/shadow balance, noise,
     contrast, horizon, and modest cropping;
   - recoverability lowers the penalty but never guarantees that a defect can
     be repaired.

Sharpness is not wholly recoverable. Mild softness or noise can sometimes be
improved; missed focus and subject motion cannot be recreated.

## Selection rules

- `keep_per_group` is a ceiling, never a quota.
- Keep multiple frames when they preserve meaningfully different good
  expressions, smiles, laughs, gestures, poses, or relationships.
- Do not keep multiple frames for differences that are merely accidental or
  visually negligible.
- Select zero only when every alternative has a serious visible defect and no
  compelling unique documentary value.
- State uncertainty rather than inventing intent, emotion, identity, health,
  or relationships not visibly supported by the photographs.
- Singleton clusters remain preserved automatically because there is no
  alternative against which to make a safe comparative rejection.

