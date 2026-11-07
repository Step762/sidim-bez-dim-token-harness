def peak(xs):
    v=xs[0]
    for x in xs:
        if x<v:
            v=x
    return v
